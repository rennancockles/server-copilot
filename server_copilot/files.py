import asyncio
import os
import time
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from server_copilot.disk_usage import DiskUsageError, resolve_target

DEFAULT_LIMIT = 100
MAX_LIMIT = 500
MAX_SCANNED = 20_000
DEFAULT_TIME_LIMIT = 10.0

Kind = Literal["file", "directory", "symlink", "other"]
SortKey = Literal["name", "size", "modified"]


class FileListingError(DiskUsageError):
    """The directory cannot be listed; the message is safe to show to the MCP client."""


class FileEntry(BaseModel):
    name: str
    kind: Kind
    size_bytes: int | None
    modified: str | None
    readable: bool | None


class FileListing(BaseModel):
    disk: str
    path: str
    entries: list[FileEntry]
    returned: int
    total_entries: int
    offset: int
    has_more: bool
    hidden_omitted: int
    scan_truncated: bool
    notes: list[str]


def _is_hidden(name: str) -> bool:
    return name.startswith(".")


def _refuse_hidden_path(root: str, target: str, path: str, include_hidden: bool) -> None:
    """Hidden entries are not listed, so a hidden directory must not be reachable by name either
    (directly or through a symlink that resolves to one)."""
    if include_hidden:
        return
    requested = PurePosixPath(path).parts
    resolved = [part for part in os.path.relpath(target, root).split(os.sep) if part != "."]
    if any(_is_hidden(part) for part in (*requested, *resolved)):
        raise FileListingError(
            "That path goes through a hidden directory (a name starting with '.'). "
            "Set include_hidden=true to list it."
        )


def _describe(entry: os.DirEntry[str]) -> FileEntry:
    try:
        if entry.is_symlink():
            kind: Kind = "symlink"
        elif entry.is_dir(follow_symlinks=False):
            kind = "directory"
        elif entry.is_file(follow_symlinks=False):
            kind = "file"
        else:
            kind = "other"
        stat = entry.stat(follow_symlinks=False)
    except OSError:
        # Gone or unreadable between listing and inspecting it.
        return FileEntry(
            name=entry.name, kind="other", size_bytes=None, modified=None, readable=False
        )
    return FileEntry(
        name=entry.name,
        kind=kind,
        # A directory's own size is meaningless and a symlink's would be the link text: use
        # get_disk_usage for the size of a tree.
        size_bytes=stat.st_size if kind == "file" else None,
        modified=datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(timespec="seconds"),
        # os.access cannot test a link itself; probing it would test its target.
        readable=None if kind == "symlink" else os.access(entry.path, os.R_OK),
    )


def _read_directory(
    target: str, *, include_hidden: bool, time_limit: float
) -> tuple[list[FileEntry], int, int, bool]:
    deadline = time.monotonic() + time_limit
    entries: list[FileEntry] = []
    hidden_omitted = 0
    scanned = 0
    truncated = False
    try:
        with os.scandir(target) as iterator:
            for entry in iterator:
                if scanned >= MAX_SCANNED or time.monotonic() >= deadline:
                    truncated = True
                    break
                scanned += 1
                if not include_hidden and _is_hidden(entry.name):
                    hidden_omitted += 1
                    continue
                entries.append(_describe(entry))
    except PermissionError as exc:
        raise FileListingError("Permission denied reading that directory.") from exc
    except OSError as exc:
        raise FileListingError(f"Cannot read that directory: {exc.strerror or exc}.") from exc
    return entries, hidden_omitted, scanned, truncated


def _sorted(entries: list[FileEntry], sort: SortKey) -> list[FileEntry]:
    by_name = sorted(entries, key=lambda e: (e.name.casefold(), e.name))
    if sort == "size":
        return sorted(
            by_name, key=lambda e: -1 if e.size_bytes is None else e.size_bytes, reverse=True
        )
    if sort == "modified":
        return sorted(by_name, key=lambda e: e.modified or "", reverse=True)
    return by_name


def list_directory(  # noqa: PLR0913
    disks: dict[str, str],
    disk: str,
    path: str = "",
    *,
    sort: SortKey = "name",
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    include_hidden: bool = False,
    time_limit: float = DEFAULT_TIME_LIMIT,
) -> FileListing:
    root, target = resolve_target(disks, disk, path)
    _refuse_hidden_path(root, target, path, include_hidden)
    entries, hidden_omitted, scanned, truncated = _read_directory(
        target, include_hidden=include_hidden, time_limit=time_limit
    )
    ordered = _sorted(entries, sort)
    page = ordered[offset : offset + limit]
    has_more = offset + len(page) < len(ordered)
    notes = []
    if truncated:
        notes.append(
            f"Only the first {scanned} entries of this directory were read (limit "
            f"{MAX_SCANNED} entries or {time_limit:g}s), so sorting and totals are partial. "
            "Use get_disk_usage to see sizes or look in a subdirectory."
        )
    if hidden_omitted:
        notes.append(f"{hidden_omitted} hidden entries are not shown; set include_hidden=true.")
    if has_more:
        notes.append(f"More entries follow: call again with offset={offset + len(page)}.")
    relative = os.path.relpath(target, root)
    return FileListing(
        disk=disk,
        path="." if relative == "." else relative,
        entries=page,
        returned=len(page),
        total_entries=len(ordered),
        offset=offset,
        has_more=has_more,
        hidden_omitted=hidden_omitted,
        scan_truncated=truncated,
        notes=notes,
    )


def register_file_tools(mcp: MCPServer, disks: dict[str, str]) -> None:
    available = ", ".join(sorted(disks)) or "none configured"

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def list_files(  # noqa: PLR0913, PLR0917
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
        sort: Annotated[
            SortKey,
            Field(description="name (A to Z), size (largest first) or modified (newest first)."),
        ] = "name",
        limit: Annotated[
            int, Field(ge=1, le=MAX_LIMIT, description="How many entries to return.")
        ] = DEFAULT_LIMIT,
        offset: Annotated[
            int, Field(ge=0, description="How many entries to skip, to read the next page.")
        ] = 0,
        include_hidden: Annotated[
            bool,
            Field(
                description="Also list (and allow entering) names starting with '.', which are "
                "left out by default because they often hold credentials."
            ),
        ] = False,
    ) -> FileListing:
        """List the contents of one directory of a configured disk: names, types, sizes and dates.

        Read-only: it never reads file contents, never follows symlinks (they are listed as
        symlinks, without their target) and cannot leave the disk. A directory's own size is not
        shown; use get_disk_usage for that.
        """
        try:
            return await asyncio.to_thread(
                list_directory,
                disks,
                disk,
                path,
                sort=sort,
                limit=limit,
                offset=offset,
                include_hidden=include_hidden,
            )
        except DiskUsageError as exc:
            raise ToolError(str(exc)) from exc
