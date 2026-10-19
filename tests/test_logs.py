import struct
from collections.abc import Callable
from datetime import UTC, datetime

import httpx2
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult

from server_copilot import logs
from server_copilot.docker import DockerClient, DockerError, DockerNotFoundError
from server_copilot.logs import demultiplex, fetch_container_logs, parse_time
from server_copilot.server import create_server

pytestmark = pytest.mark.unit

Handler = Callable[[httpx2.Request], httpx2.Response]

NOW = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)


def frame(stream: int, text: str) -> bytes:
    payload = text.encode()
    return struct.pack(">BxxxI", stream, len(payload)) + payload


def routed(
    body: bytes, *, tty: bool = False, on_logs: Callable[[httpx2.Request], None] | None = None
) -> Handler:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/json"):
            return httpx2.Response(200, json={"Config": {"Tty": tty}})
        if on_logs:
            on_logs(request)
        return httpx2.Response(200, content=body)

    return handler


def make_client(handler: Handler) -> DockerClient:
    return DockerClient("tcp://proxy:2375", transport=httpx2.MockTransport(handler))


def ts(n: int) -> str:
    return f"2026-10-19T11:00:{n:02d}.000000000Z"


def test_parse_time_relative() -> None:
    assert parse_time("30m", NOW) == int(NOW.timestamp()) - 1800
    assert parse_time("2H", NOW) == int(NOW.timestamp()) - 7200
    assert parse_time("1d", NOW) == int(NOW.timestamp()) - 86400
    assert parse_time("1w", NOW) == int(NOW.timestamp()) - 7 * 86400


def test_parse_time_iso() -> None:
    expected = int(datetime(2026, 10, 19, 8, 0, tzinfo=UTC).timestamp())
    assert parse_time("2026-10-19T08:00:00Z", NOW) == expected
    assert parse_time("2026-10-19T08:00:00", NOW) == expected
    assert parse_time("2026-10-19T05:00:00-03:00", NOW) == expected


@pytest.mark.parametrize("value", ["yesterday", "10", "5x", ""])
def test_parse_time_rejects_garbage(value: str) -> None:
    with pytest.raises(DockerError, match="Invalid time"):
        parse_time(value, NOW)


def test_demultiplex_filters_streams() -> None:
    data = frame(1, "out-1\n") + frame(2, "err-1\n") + frame(1, "out-2\n")
    assert demultiplex(data, "all") == "out-1\nerr-1\nout-2\n"
    assert demultiplex(data, "stdout") == "out-1\nout-2\n"
    assert demultiplex(data, "stderr") == "err-1\n"


def test_demultiplex_tolerates_cut_frames() -> None:
    data = frame(1, "complete\n") + frame(1, "partial line")[:-5]
    assert demultiplex(data, "all") == "complete\npartial"


async def test_returns_lines_and_sends_query_params() -> None:
    seen: dict[str, str] = {}

    def on_logs(request: httpx2.Request) -> None:
        seen.update(request.url.params)

    body = frame(1, f"{ts(1)} hello\n") + frame(2, f"{ts(2)} boom\n")
    async with make_client(routed(body, on_logs=on_logs)) as docker:
        result = await fetch_container_logs(
            docker, "app", tail=50, since="1h", until="10m", now=NOW
        )

    assert result.logs == f"{ts(1)} hello\n{ts(2)} boom"
    assert result.returned_lines == 2
    assert result.truncated is False
    assert result.notes == []
    assert seen["tail"] == "50"
    assert seen["stdout"] == "1"
    assert seen["stderr"] == "1"
    assert seen["timestamps"] == "1"
    assert seen["since"] == str(int(NOW.timestamp()) - 3600)
    assert seen["until"] == str(int(NOW.timestamp()) - 600)


async def test_stream_selection_and_timestamps_flags() -> None:
    seen: dict[str, str] = {}
    body = frame(1, "out\n") + frame(2, "err\n")
    async with make_client(routed(body, on_logs=lambda r: seen.update(r.url.params))) as docker:
        result = await fetch_container_logs(docker, "app", streams="stderr", timestamps=False)

    assert result.logs == "err"
    assert seen["stdout"] == "0"
    assert seen["stderr"] == "1"
    assert seen["timestamps"] == "0"


async def test_tty_container_logs_are_plain_text() -> None:
    async with make_client(routed(b"plain 1\nplain 2\n", tty=True)) as docker:
        result = await fetch_container_logs(docker, "tty-app", timestamps=False)

    assert result.logs == "plain 1\nplain 2"


async def test_text_filter_is_case_insensitive_and_ignores_timestamp() -> None:
    seen: dict[str, str] = {}
    body = frame(
        1,
        f"{ts(1)} GET /health 200\n{ts(2)} ERROR db down\n{ts(3)} ok\n{ts(4)} error again\n",
    )
    async with make_client(routed(body, on_logs=lambda r: seen.update(r.url.params))) as docker:
        result = await fetch_container_logs(docker, "app", tail=1, search="error")
        by_timestamp = await fetch_container_logs(docker, "app", search="2026-10-19")

    assert result.logs == f"{ts(4)} error again"
    assert seen["tail"] == str(logs.SCAN_LINES)  # scan window, then tail applied after filtering
    assert by_timestamp.logs == ""


async def test_text_filter_escapes_regex_characters() -> None:
    body = frame(1, "a.b\naxb\n")
    async with make_client(routed(body)) as docker:
        result = await fetch_container_logs(docker, "app", search="a.b", timestamps=False)

    assert result.logs == "a.b"


async def test_regex_filter() -> None:
    body = frame(1, "status=200\nstatus=503\nstatus=404\nstatus=500\n")
    async with make_client(routed(body)) as docker:
        result = await fetch_container_logs(
            docker, "app", search=r"status=5\d\d", regex=True, timestamps=False
        )

    assert result.logs == "status=503\nstatus=500"


async def test_invalid_regex_is_reported() -> None:
    async with make_client(routed(b"")) as docker:
        with pytest.raises(DockerError, match="Invalid regular expression"):
            await fetch_container_logs(docker, "app", search="(", regex=True)


async def test_output_is_truncated_to_max_bytes_keeping_newest_lines() -> None:
    body = frame(1, "".join(f"line-{n:03d}\n" for n in range(100)))
    async with make_client(routed(body)) as docker:
        result = await fetch_container_logs(docker, "app", timestamps=False, max_bytes=50)

    lines = result.logs.splitlines()
    assert lines == [f"line-{n:03d}" for n in range(95, 100)]
    assert len(result.logs.encode()) <= 50
    assert result.truncated is True
    assert result.omitted_lines == 95
    assert "50 bytes" in result.notes[0]


async def test_single_oversized_line_is_cut() -> None:
    body = frame(1, "x" * 5000 + "\n")
    async with make_client(routed(body)) as docker:
        result = await fetch_container_logs(docker, "app", timestamps=False, max_bytes=100)

    assert result.logs.endswith("...[line truncated]")
    assert result.returned_lines == 1
    assert result.truncated is False


async def test_note_when_filter_scan_window_is_full(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(logs, "SCAN_LINES", 3)
    body = frame(1, "a\nb\nc\n")
    async with make_client(routed(body)) as docker:
        result = await fetch_container_logs(docker, "app", search="c", timestamps=False)

    assert result.logs == "c"
    assert "last 3 lines" in result.notes[0]


async def test_note_when_fetch_hits_byte_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(logs, "MAX_FETCH_BYTES", 40)
    body = frame(1, "".join(f"line-{n}\n" for n in range(50)))
    async with make_client(routed(body)) as docker:
        result = await fetch_container_logs(docker, "app", timestamps=False)

    assert result.truncated is True
    assert any("oldest part" in note for note in result.notes)


async def test_unknown_container_is_not_found() -> None:
    handler = lambda request: httpx2.Response(  # noqa: E731
        404, json={"message": "No such container: ghost"}
    )
    async with make_client(handler) as docker:
        with pytest.raises(DockerNotFoundError, match="ghost"):
            await fetch_container_logs(docker, "ghost")


async def test_container_reference_is_escaped_and_validated() -> None:
    paths: list[bytes] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        paths.append(request.url.raw_path)
        return httpx2.Response(404, json={"message": "nope"})

    async with make_client(handler) as docker:
        with pytest.raises(DockerNotFoundError):
            await fetch_container_logs(docker, "../images/json")
        with pytest.raises(DockerError, match="must not be empty"):
            await fetch_container_logs(docker, "  ")

    assert paths[0].startswith(b"/containers/..%2Fimages%2Fjson/")


async def test_logs_http_error_is_reported() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/json"):
            return httpx2.Response(200, json={"Config": {}})
        return httpx2.Response(500, json={"message": "log driver does not support reading"})

    async with make_client(handler) as docker:
        with pytest.raises(DockerError, match="log driver does not support reading"):
            await fetch_container_logs(docker, "app")


async def test_tool_is_registered_with_bounded_schema() -> None:
    mcp = create_server(make_client(routed(b"")))
    tool = next(t for t in await mcp.list_tools() if t.name == "get_container_logs")

    properties = tool.input_schema["properties"]
    assert set(properties) == {
        "container",
        "tail",
        "since",
        "until",
        "search",
        "regex",
        "streams",
        "timestamps",
        "max_bytes",
    }
    assert all(prop.get("description") for prop in properties.values())
    assert properties["tail"]["default"] == logs.DEFAULT_TAIL
    assert properties["tail"]["maximum"] == logs.MAX_TAIL
    assert properties["max_bytes"]["maximum"] == logs.MAX_MAX_BYTES
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True


async def test_tool_returns_structured_logs() -> None:
    mcp = create_server(make_client(routed(frame(1, "hello\n"))))

    result = await mcp.call_tool("get_container_logs", {"container": "app", "timestamps": False})

    assert isinstance(result, CallToolResult)
    assert result.structured_content is not None
    assert result.structured_content["logs"] == "hello"
    assert result.structured_content["truncated"] is False


async def test_tool_rejects_out_of_range_tail() -> None:
    mcp = create_server(make_client(routed(b"")))

    with pytest.raises(ToolError):
        await mcp.call_tool("get_container_logs", {"container": "app", "tail": 100000})


async def test_tool_surfaces_errors_as_tool_errors() -> None:
    mcp = create_server(make_client(routed(b"")))

    with pytest.raises(ToolError, match="Invalid time"):
        await mcp.call_tool("get_container_logs", {"container": "app", "since": "soon"})
