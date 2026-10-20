import asyncio
import contextlib
import heapq
import os
import time
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

DEFAULT_LIMIT = 25
MAX_LIMIT = 100
MAX_DEPTH = 4
DEFAULT_TIMEOUT = 20
MIN_TIMEOUT = 5
MAX_TIMEOUT = 60
MAX_ERROR_SAMPLES = 3
NICE_LEVEL = "10"
PERCENT_DIGITS = 1
DRAIN_SECONDS = 2
# du exits 1 when it could not read some entries but still produced a result.
DU_PARTIAL_EXIT_CODES = (0, 1)


class DiskUsageError(Exception):
    """The scan cannot run; the message is safe to show to the MCP client."""


class DiskEntry(BaseModel):
    path: str
    size_bytes: int
    percent_of_total: float | None
    kind: Literal["directory", "file"]


class DiskUsageReport(BaseModel):
    disk: str
    path: str
    total_bytes: int | None
    entries: list[DiskEntry]
    returned: int
    omitted: int
    complete: bool
    unreadable: int
    elapsed_seconds: float
    notes: list[str]


@dataclass
class _Scan:
    total: int | None = None
    seen: int = 0
    top: list[tuple[int, str]] = field(default_factory=list)
    unreadable: int = 0
    error_samples: list[str] = field(default_factory=list)
    other_errors: list[str] = field(default_factory=list)
    complete: bool = True


def resolve_target(disks: dict[str, str], disk: str, path: str) -> tuple[str, str]:
    """Return (real disk root, real target directory), refusing anything outside the disk."""
    if not disks:
        raise DiskUsageError(
            "No disks are configured: set SERVER_COPILOT_HOST_DISKS=name=/container/path,... "
            "and bind-mount each host path read-only."
        )
    if disk not in disks:
        raise DiskUsageError(f"Unknown disk {disk!r}; available: {', '.join(sorted(disks))}.")
    if "\0" in path:
        raise DiskUsageError("The path contains an invalid character.")
    relative = PurePosixPath(path)
    if relative.is_absolute():
        raise DiskUsageError(
            "path is relative to the disk root (for example 'Containers/plex'), not absolute."
        )
    if ".." in relative.parts:
        raise DiskUsageError("path must not contain '..'.")
    root = os.path.realpath(disks[disk])
    if not os.path.isdir(root):
        raise DiskUsageError(f"Disk {disk!r} is not mounted in the container.")
    target = os.path.realpath(os.path.join(root, *relative.parts))
    if os.path.commonpath([root, target]) != root:
        raise DiskUsageError("path resolves outside the disk.")
    if not os.path.isdir(target):
        raise DiskUsageError(f"{path or '.'!r} is not a directory on disk {disk!r}.")
    return root, target


def _relative(path: str, root: str) -> str:
    return os.path.relpath(path, root)


def _du_command(
    target: str, *, max_depth: int, include_files: bool, cross_filesystems: bool
) -> list[str]:
    command = [
        "nice",
        "-n",
        NICE_LEVEL,
        "du",
        "--block-size=1",
        "--no-dereference",
        f"--max-depth={max_depth}",
    ]
    if include_files:
        command.append("--all")
    if not cross_filesystems:
        command.append("--one-file-system")
    return [*command, "--", target]


async def _read_entries(stream: asyncio.StreamReader, scan: _Scan, target: str, limit: int) -> None:
    async for raw in stream:
        size_text, _, name = raw.decode(errors="replace").rstrip("\n").partition("\t")
        if not size_text.isdigit() or not name:
            continue
        size = int(size_text)
        if name == target:
            scan.total = size
            continue
        scan.seen += 1
        if len(scan.top) < limit:
            heapq.heappush(scan.top, (size, name))
        elif size > scan.top[0][0]:
            heapq.heapreplace(scan.top, (size, name))


async def _read_errors(stream: asyncio.StreamReader, scan: _Scan, root: str) -> None:
    async for raw in stream:
        message = raw.decode(errors="replace").strip().replace(f"{root}/", "").replace(root, ".")
        # Only du's own "du: cannot read ..." lines are unreadable entries; anything else
        # (for example a warning from nice) is not a file we failed to measure.
        if message.startswith("du:"):
            scan.unreadable += 1
            if len(scan.error_samples) < MAX_ERROR_SAMPLES:
                scan.error_samples.append(message)
        elif len(scan.other_errors) < MAX_ERROR_SAMPLES:
            scan.other_errors.append(message)


async def _run_du(
    target: str, root: str, *, limit: int, time_limit: float, command: list[str]
) -> _Scan:
    scan = _Scan()
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={"LC_ALL": "C", "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        )
    except FileNotFoundError as exc:
        raise DiskUsageError("du is not available in this environment.") from exc
    assert process.stdout is not None
    assert process.stderr is not None
    readers = asyncio.gather(
        _read_entries(process.stdout, scan, target, limit),
        _read_errors(process.stderr, scan, root),
    )
    try:
        async with asyncio.timeout(time_limit):
            # shield: hitting the deadline must not cancel the readers, which still have to parse
            # what du already wrote.
            await asyncio.shield(readers)
            await process.wait()
    except TimeoutError:
        scan.complete = False
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        await process.wait()
        # With du gone the pipes reach EOF, so the readers finish on their own.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(readers, DRAIN_SECONDS)
    if scan.complete and process.returncode not in DU_PARTIAL_EXIT_CODES:
        errors = scan.error_samples or scan.other_errors
        detail = errors[0] if errors else f"exit code {process.returncode}"
        raise DiskUsageError(f"du failed: {detail}")
    return scan


def _entry(size: int, path: str, root: str, total: int | None) -> DiskEntry:
    is_directory = os.path.isdir(path) and not os.path.islink(path)
    return DiskEntry(
        path=_relative(path, root),
        size_bytes=size,
        percent_of_total=round(size / total * 100, PERCENT_DIGITS) if total else None,
        kind="directory" if is_directory else "file",
    )


async def measure_disk_usage(  # noqa: PLR0913
    disks: dict[str, str],
    disk: str,
    path: str = "",
    *,
    max_depth: int = 1,
    limit: int = DEFAULT_LIMIT,
    include_files: bool = True,
    cross_filesystems: bool = False,
    time_limit: float = DEFAULT_TIMEOUT,
) -> DiskUsageReport:
    root, target = resolve_target(disks, disk, path)
    command = _du_command(
        target,
        max_depth=max_depth,
        include_files=include_files,
        cross_filesystems=cross_filesystems,
    )
    started = time.monotonic()
    scan = await _run_du(target, root, limit=limit, time_limit=time_limit, command=command)
    elapsed = time.monotonic() - started

    entries = [
        _entry(size, name, root, scan.total) for size, name in sorted(scan.top, reverse=True)
    ]
    omitted = max(scan.seen - len(entries), 0)
    notes = []
    if not scan.complete:
        notes.append(
            f"Stopped after {time_limit:g}s: the entries listed are complete, but the total and "
            "some entries are missing. Narrow 'path', lower 'max_depth' or raise 'timeout_seconds'."
        )
    if scan.unreadable:
        samples = "; ".join(scan.error_samples)
        notes.append(
            f"{scan.unreadable} entries could not be read (permissions or files that vanished), "
            f"so their sizes are not counted. For example: {samples}"
        )
    if omitted:
        notes.append(f"Showing the {len(entries)} largest of {scan.seen} entries.")
    return DiskUsageReport(
        disk=disk,
        path=_relative(target, root) if target != root else ".",
        total_bytes=scan.total,
        entries=entries,
        returned=len(entries),
        omitted=omitted,
        complete=scan.complete,
        unreadable=scan.unreadable,
        elapsed_seconds=round(elapsed, 2),
        notes=notes,
    )


def register_disk_tools(mcp: MCPServer, disks: dict[str, str]) -> None:
    available = ", ".join(sorted(disks)) or "none configured"
    scan_lock = asyncio.Lock()

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def get_disk_usage(  # noqa: PLR0913, PLR0917
        disk: Annotated[
            str, Field(description=f"Configured disk to look inside. Available: {available}.")
        ],
        path: Annotated[
            str,
            Field(
                description="Directory inside the disk, relative to its root (for example "
                "'Containers/plex'). Empty means the root."
            ),
        ] = "",
        max_depth: Annotated[
            int,
            Field(
                ge=1,
                le=MAX_DEPTH,
                description="How many directory levels below 'path' to report (1 = its children).",
            ),
        ] = 1,
        limit: Annotated[
            int, Field(ge=1, le=MAX_LIMIT, description="How many of the largest entries to return.")
        ] = DEFAULT_LIMIT,
        include_files: Annotated[
            bool, Field(description="Also list single files, not only directories.")
        ] = True,
        cross_filesystems: Annotated[
            bool,
            Field(
                description="Also count other filesystems mounted below 'path' (for example an "
                "external drive). By default they are skipped, and so missing from the list."
            ),
        ] = False,
        timeout_seconds: Annotated[
            int,
            Field(
                ge=MIN_TIMEOUT,
                le=MAX_TIMEOUT,
                description="Stop the scan after this long and return what finished.",
            ),
        ] = DEFAULT_TIMEOUT,
    ) -> DiskUsageReport:
        """Find what takes the most space inside a configured disk, largest first.

        Read-only: it reports names and sizes, never file contents, never follows symlinks and
        runs one scan at a time at low priority. Drill down by calling it again with the
        'path' of a large directory from the previous answer.
        """
        if scan_lock.locked():
            raise ToolError("Another disk scan is still running; try again when it finishes.")
        async with scan_lock:
            try:
                return await measure_disk_usage(
                    disks,
                    disk,
                    path,
                    max_depth=max_depth,
                    limit=limit,
                    include_files=include_files,
                    cross_filesystems=cross_filesystems,
                    time_limit=timeout_seconds,
                )
            except DiskUsageError as exc:
                raise ToolError(str(exc)) from exc
