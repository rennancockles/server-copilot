import re
import struct
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from urllib.parse import quote

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from server_copilot.docker import DockerClient, DockerError

LogStreams = Literal["all", "stdout", "stderr"]

DEFAULT_TAIL = 200
MAX_TAIL = 2000
DEFAULT_MAX_BYTES = 20_000
MAX_MAX_BYTES = 100_000
# When filtering, Docker's `tail` would apply before the filter, so scan this many lines instead.
SCAN_LINES = 5000
MAX_FETCH_BYTES = 16 * 1024 * 1024
MAX_MATCH_LINE_LENGTH = 4000

_FRAME_HEADER = struct.Struct(">BxxxI")
_STDOUT = 1
_STDERR = 2
_RELATIVE_TIME = re.compile(r"^(\d+)\s*([smhdw])$", re.IGNORECASE)
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


class ContainerLogs(BaseModel):
    container: str
    logs: str
    returned_lines: int
    truncated: bool
    omitted_lines: int
    notes: list[str]


def parse_time(value: str, now: datetime | None = None) -> int:
    """Turn '30m'/'2h'/'1d' (relative to now) or an ISO 8601 timestamp into epoch seconds."""
    now = now or datetime.now(UTC)
    text = value.strip()
    if match := _RELATIVE_TIME.match(text):
        delta = timedelta(**{_UNITS[match.group(2).lower()]: int(match.group(1))})
        return int((now - delta).timestamp())
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise DockerError(
            f"Invalid time {value!r}: use a relative value like '30m', '2h', '1d' "
            "or an ISO 8601 timestamp like '2026-10-19T08:00:00Z'."
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


def demultiplex(data: bytes, streams: LogStreams) -> str:
    """Decode Docker's multiplexed log stream (8-byte frame headers) into text."""
    wanted = {"all": {_STDOUT, _STDERR}, "stdout": {_STDOUT}, "stderr": {_STDERR}}[streams]
    parts: list[bytes] = []
    offset = 0
    header_size = _FRAME_HEADER.size
    while offset + header_size <= len(data):
        stream_type, size = _FRAME_HEADER.unpack_from(data, offset)
        offset += header_size
        if stream_type in wanted:
            parts.append(data[offset : offset + size])
        offset += size
    return b"".join(parts).decode("utf-8", errors="replace")


def _compile_filter(search: str, regex: bool) -> re.Pattern[str]:
    try:
        return re.compile(search if regex else re.escape(search), re.IGNORECASE)
    except re.error as exc:
        raise DockerError(f"Invalid regular expression {search!r}: {exc}") from None


def _message(line: str, timestamps: bool) -> str:
    message = line.partition(" ")[2] if timestamps else line
    return message[:MAX_MATCH_LINE_LENGTH]


def _fit_to_budget(lines: list[str], max_bytes: int) -> tuple[list[str], int]:
    """Keep the newest lines that fit in max_bytes. Returns (lines, omitted_count)."""
    kept: list[str] = []
    used = 0
    for line in reversed(lines):
        size = len(line.encode()) + 1
        if used + size > max_bytes:
            if not kept:
                trimmed = line.encode()[:max_bytes].decode("utf-8", errors="ignore")
                kept.append(f"{trimmed}...[line truncated]")
            break
        kept.append(line)
        used += size
    kept.reverse()
    return kept, len(lines) - len(kept)


async def fetch_container_logs(  # noqa: PLR0913
    docker: DockerClient,
    container: str,
    *,
    tail: int = DEFAULT_TAIL,
    since: str | None = None,
    until: str | None = None,
    search: str | None = None,
    regex: bool = False,
    streams: LogStreams = "all",
    timestamps: bool = True,
    max_bytes: int = DEFAULT_MAX_BYTES,
    now: datetime | None = None,
) -> ContainerLogs:
    container = container.strip()
    if not container:
        raise DockerError("Container name or ID must not be empty.")
    pattern = _compile_filter(search, regex) if search else None
    params: dict[str, str | int] = {
        "stdout": int(streams != "stderr"),
        "stderr": int(streams != "stdout"),
        "timestamps": int(timestamps),
        "tail": SCAN_LINES if pattern else tail,
    }
    if since:
        params["since"] = parse_time(since, now)
    if until:
        params["until"] = parse_time(until, now)

    reference = quote(container, safe="")
    info = await docker.get_json(f"/containers/{reference}/json")
    is_tty = bool((info.get("Config") or {}).get("Tty"))
    data, fetch_capped = await docker.get_bytes(
        f"/containers/{reference}/logs", params, max_bytes=MAX_FETCH_BYTES
    )
    text = data.decode("utf-8", errors="replace") if is_tty else demultiplex(data, streams)

    lines = text.splitlines()
    notes: list[str] = []
    scanned = len(lines)
    if pattern:
        lines = [line for line in lines if pattern.search(_message(line, timestamps))]
        lines = lines[-tail:]
        if scanned >= SCAN_LINES:
            notes.append(
                f"Only the last {SCAN_LINES} lines in the time window were searched; "
                "narrow the window with 'since' to look further back."
            )
    if fetch_capped:
        notes.append(
            f"Docker returned more than {MAX_FETCH_BYTES // (1024 * 1024)} MiB; only the oldest "
            "part of the window was read. Use 'since' or a smaller 'tail'."
        )

    kept, omitted = _fit_to_budget(lines, max_bytes)
    if omitted:
        notes.append(
            f"Output limited to {max_bytes} bytes: the {omitted} oldest matching lines were "
            "omitted. Raise 'max_bytes' or narrow the window."
        )
    return ContainerLogs(
        container=container,
        logs="\n".join(kept),
        returned_lines=len(kept),
        truncated=bool(omitted) or fetch_capped,
        omitted_lines=omitted,
        notes=notes,
    )


def register_log_tools(mcp: MCPServer, docker: DockerClient) -> None:
    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def get_container_logs(  # noqa: PLR0913, PLR0917
        container: Annotated[str, Field(description="Container name or ID.")],
        tail: Annotated[
            int,
            Field(
                ge=1,
                le=MAX_TAIL,
                description="Maximum number of (most recent) lines to return.",
            ),
        ] = DEFAULT_TAIL,
        since: Annotated[
            str | None,
            Field(
                description="Only logs after this time: relative ('30m', '2h', '1d') "
                "or ISO 8601 ('2026-10-19T08:00:00Z', UTC if no timezone)."
            ),
        ] = None,
        until: Annotated[
            str | None,
            Field(description="Only logs before this time (same formats as 'since')."),
        ] = None,
        search: Annotated[
            str | None,
            Field(
                description="Only lines containing this text (case-insensitive), "
                "or matching it when 'regex' is true."
            ),
        ] = None,
        regex: Annotated[
            bool, Field(description="Treat 'search' as a regular expression.")
        ] = False,
        streams: Annotated[
            LogStreams, Field(description="Which output streams to include.")
        ] = "all",
        timestamps: Annotated[
            bool, Field(description="Prefix each line with its timestamp.")
        ] = True,
        max_bytes: Annotated[
            int,
            Field(
                ge=1_000,
                le=MAX_MAX_BYTES,
                description="Maximum size of the returned text; oldest lines are dropped first.",
            ),
        ] = DEFAULT_MAX_BYTES,
    ) -> ContainerLogs:
        """Get recent logs of a container, optionally filtered by time window and text/regex.

        Returns the newest lines that fit the limits and notes when output was truncated.
        """
        try:
            return await fetch_container_logs(
                docker,
                container,
                tail=tail,
                since=since,
                until=until,
                search=search,
                regex=regex,
                streams=streams,
                timestamps=timestamps,
                max_bytes=max_bytes,
            )
        except DockerError as exc:
            raise ToolError(str(exc)) from exc
