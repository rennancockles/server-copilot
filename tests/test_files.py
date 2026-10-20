import errno
import os
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Self

import httpx2
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult

from server_copilot import files
from server_copilot.disk_usage import DiskUsageError
from server_copilot.docker import DockerClient
from server_copilot.files import FileEntry, FileListingError, SortKey, list_directory
from server_copilot.server import create_server

pytestmark = pytest.mark.unit

is_root = hasattr(os, "geteuid") and os.geteuid() == 0


def make_client() -> DockerClient:
    return DockerClient(
        "tcp://proxy:2375", transport=httpx2.MockTransport(lambda r: httpx2.Response(200))
    )


@pytest.fixture
def home(tmp_path: Path) -> Path:
    root = Path(os.path.realpath(tmp_path)) / "home"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / ".cache").mkdir()
    (root / "docs" / ".cache" / "token").write_text("secret")
    (root / "notes.txt").write_bytes(b"hello")
    (root / "Big.bin").write_bytes(b"x" * 1000)
    (root / ".env").write_text("PASSWORD=hunter2")
    (root / ".ssh").mkdir()
    (root / ".ssh" / "id_rsa").write_text("key")
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "outside").symlink_to(outside)
    (root / "link").symlink_to(root / "docs")
    return root


def listing(  # noqa: PLR0913
    home: Path,
    path: str = "",
    *,
    sort: SortKey = "name",
    limit: int = files.DEFAULT_LIMIT,
    offset: int = 0,
    include_hidden: bool = False,
    time_limit: float = files.DEFAULT_TIME_LIMIT,
) -> files.FileListing:
    return list_directory(
        {"home": str(home)},
        "home",
        path,
        sort=sort,
        limit=limit,
        offset=offset,
        include_hidden=include_hidden,
        time_limit=time_limit,
    )


def names(result: files.FileListing) -> list[str]:
    return [entry.name for entry in result.entries]


def test_lists_names_kinds_sizes_and_dates(home: Path) -> None:
    result = listing(home)

    assert names(result) == ["Big.bin", "docs", "link", "notes.txt", "outside"]
    by_name = {entry.name: entry for entry in result.entries}
    assert by_name["Big.bin"].kind == "file"
    assert by_name["Big.bin"].size_bytes == 1000
    assert by_name["notes.txt"].size_bytes == 5
    assert by_name["docs"].kind == "directory"
    assert by_name["docs"].size_bytes is None
    assert by_name["link"].kind == "symlink"
    assert by_name["link"].size_bytes is None
    assert by_name["Big.bin"].readable is True
    assert by_name["link"].readable is None
    modified = by_name["notes.txt"].modified
    assert modified is not None
    assert datetime.fromisoformat(modified).utcoffset() is not None
    assert result.path == "."
    assert result.total_entries == 5


def test_entries_carry_metadata_only() -> None:
    assert set(FileEntry.model_fields) == {"name", "kind", "size_bytes", "modified", "readable"}


def test_name_sorting_ignores_case(home: Path) -> None:
    (home / "apple").write_text("a")
    (home / "Zebra").write_text("z")

    assert names(listing(home))[:3] == ["apple", "Big.bin", "docs"]
    assert names(listing(home))[-1] == "Zebra"


def test_hidden_entries_are_omitted_by_default(home: Path) -> None:
    result = listing(home)

    assert ".env" not in names(result)
    assert ".ssh" not in names(result)
    assert result.hidden_omitted == 2
    assert any("include_hidden=true" in note for note in result.notes)


def test_hidden_entries_are_listed_on_request(home: Path) -> None:
    result = listing(home, include_hidden=True)

    assert {".env", ".ssh"} <= set(names(result))
    assert result.hidden_omitted == 0


@pytest.mark.parametrize("path", [".ssh", "docs/.cache", ".ssh/"])
def test_hidden_directories_cannot_be_entered_by_name(home: Path, path: str) -> None:
    with pytest.raises(DiskUsageError, match="hidden directory"):
        listing(home, path)


def test_hidden_directories_can_be_entered_on_request(home: Path) -> None:
    result = listing(home, ".ssh", include_hidden=True)

    assert names(result) == ["id_rsa"]
    assert result.path == ".ssh"


def test_a_symlink_to_a_hidden_directory_does_not_bypass_the_rule(home: Path) -> None:
    (home / "shortcut").symlink_to(home / ".ssh")

    with pytest.raises(DiskUsageError, match="hidden directory"):
        listing(home, "shortcut")


def test_symlinks_are_listed_without_their_target(home: Path, tmp_path: Path) -> None:
    result = listing(home)

    outside = next(entry for entry in result.entries if entry.name == "outside")
    assert outside.kind == "symlink"
    assert str(tmp_path / "outside") not in result.model_dump_json()


def test_a_symlink_leaving_the_disk_cannot_be_entered(home: Path) -> None:
    with pytest.raises(DiskUsageError, match="outside the disk"):
        listing(home, "outside")


def test_a_symlink_inside_the_disk_is_resolved(home: Path) -> None:
    result = listing(home, "link", include_hidden=True)

    assert result.path == "docs"
    assert ".cache" in names(result)


def test_unsafe_paths_are_refused_like_the_disk_usage_tool(home: Path) -> None:
    for path, message in (("/etc", "relative"), ("../etc", r"'\.\.'"), ("notes.txt", "directory")):
        with pytest.raises(DiskUsageError, match=message):
            listing(home, path)


def test_size_sorting_puts_the_largest_files_first(home: Path) -> None:
    result = listing(home, sort="size")

    assert names(result) == ["Big.bin", "notes.txt", "docs", "link", "outside"]


def test_modified_sorting_puts_the_newest_first(home: Path) -> None:
    for age, name in enumerate(["outside", "link", "docs", "notes.txt", "Big.bin"]):
        stamp = 1_600_000_000 + age * 100_000_000
        os.utime(home / name, (stamp, stamp), follow_symlinks=False)

    result = listing(home, sort="modified")

    assert names(result) == ["Big.bin", "notes.txt", "docs", "link", "outside"]


def test_pagination_walks_the_whole_directory(home: Path) -> None:
    first = listing(home, limit=2)
    second = listing(home, limit=2, offset=2)
    last = listing(home, limit=2, offset=4)

    assert names(first) == ["Big.bin", "docs"]
    assert first.has_more is True
    assert any("offset=2" in note for note in first.notes)
    assert names(second) == ["link", "notes.txt"]
    assert names(last) == ["outside"]
    assert last.has_more is False
    assert {first.total_entries, second.total_entries, last.total_entries} == {5}


def test_an_offset_past_the_end_returns_nothing(home: Path) -> None:
    result = listing(home, offset=50)

    assert result.entries == []
    assert result.has_more is False


def test_huge_directories_are_read_only_up_to_a_limit(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(files, "MAX_SCANNED", 3)

    result = listing(home)

    assert result.scan_truncated is True
    assert any("first 3 entries" in note for note in result.notes)
    assert result.hidden_omitted + result.total_entries == 3


def test_a_time_limit_stops_the_read(home: Path) -> None:
    result = listing(home, time_limit=0)

    assert result.scan_truncated is True
    assert result.entries == []


@pytest.mark.skipif(is_root, reason="root ignores permission bits")
def test_a_directory_without_permission_is_reported(home: Path) -> None:
    locked = home / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        with pytest.raises(FileListingError, match="Permission denied"):
            listing(home, "locked")
    finally:
        locked.chmod(0o755)


@pytest.mark.skipif(is_root, reason="root ignores permission bits")
def test_an_unreadable_file_is_marked_not_hidden(home: Path) -> None:
    secret = home / "private.txt"
    secret.write_text("x")
    secret.chmod(0)

    entry = next(e for e in listing(home).entries if e.name == "private.txt")

    assert entry.readable is False
    assert entry.size_bytes == 1


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs mkfifo")
def test_special_files_are_reported_as_other(home: Path) -> None:
    os.mkfifo(home / "pipe")

    entry = next(e for e in listing(home).entries if e.name == "pipe")

    assert entry.kind == "other"
    assert entry.size_bytes is None


class VanishedEntry:
    name = "ghost"
    path = "/nowhere/ghost"

    def is_symlink(self) -> bool:
        return False

    def is_dir(self, *, follow_symlinks: bool = True) -> bool:
        return False

    def is_file(self, *, follow_symlinks: bool = True) -> bool:
        return False

    def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
        raise FileNotFoundError(errno.ENOENT, "gone")


class FakeScan:
    def __init__(self, entries: list[VanishedEntry]) -> None:
        self._entries = entries

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def __iter__(self) -> Self:
        return self

    def __next__(self) -> VanishedEntry:
        if not self._entries:
            raise StopIteration
        return self._entries.pop()


def test_an_entry_that_vanishes_while_listing_is_not_fatal(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(files.os, "scandir", lambda _path: FakeScan([VanishedEntry()]))

    result = listing(home)

    assert result.entries == [
        FileEntry(name="ghost", kind="other", size_bytes=None, modified=None, readable=False)
    ]


def test_other_read_errors_are_reported(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(_path: str) -> FakeScan:
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(files.os, "scandir", broken)

    with pytest.raises(FileListingError, match="Cannot read that directory: Input/output error"):
        listing(home)


async def test_tool_is_registered_with_bounded_arguments(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SERVER_COPILOT_HOST_DISKS", f"home={home},data={home}")
    mcp = create_server(make_client())

    tool = next(t for t in await mcp.list_tools() if t.name == "list_files")

    properties = tool.input_schema["properties"]
    assert set(properties) == {"disk", "path", "sort", "limit", "offset", "include_hidden"}
    assert all(prop.get("description") for prop in properties.values())
    assert "data, home" in properties["disk"]["description"]
    assert properties["limit"]["maximum"] == files.MAX_LIMIT
    assert properties["limit"]["default"] == files.DEFAULT_LIMIT
    assert properties["include_hidden"]["default"] is False
    assert tool.input_schema["required"] == ["disk"]
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True


async def test_tool_returns_a_structured_listing(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SERVER_COPILOT_HOST_DISKS", f"home={home}")
    mcp = create_server(make_client())

    result = await mcp.call_tool("list_files", {"disk": "home", "sort": "size", "limit": 2})

    assert isinstance(result, CallToolResult)
    assert result.structured_content is not None
    assert [e["name"] for e in result.structured_content["entries"]] == ["Big.bin", "notes.txt"]
    assert result.structured_content["has_more"] is True


async def test_tool_surfaces_errors_as_tool_errors(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SERVER_COPILOT_HOST_DISKS", raising=False)
    with pytest.raises(ToolError, match="SERVER_COPILOT_HOST_DISKS"):
        await create_server(make_client()).call_tool("list_files", {"disk": "home"})

    monkeypatch.setenv("SERVER_COPILOT_HOST_DISKS", f"home={home}")
    mcp = create_server(make_client())
    with pytest.raises(ToolError, match="hidden directory"):
        await mcp.call_tool("list_files", {"disk": "home", "path": ".ssh"})
    with pytest.raises(ToolError, match="Unknown disk"):
        await mcp.call_tool("list_files", {"disk": "nope"})
    with pytest.raises(ToolError):
        await mcp.call_tool("list_files", {"disk": "home", "limit": 100000})
    with pytest.raises(ToolError):
        await mcp.call_tool("list_files", {"disk": "home", "offset": -1})
