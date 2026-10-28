import logging
import os
from pathlib import Path

import httpx2
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult

from server_copilot import text_files
from server_copilot.docker import DockerClient
from server_copilot.host import parse_disks
from server_copilot.server import create_server
from server_copilot.text_files import TextFile, TextFileError, read_text_file

pytestmark = pytest.mark.unit

RUNBOOK = """# Disaster recovery runbook

## Procedure

1. Restore the dumps.
2. Run `make up`.

### The from-scratch drill (does the password manager suffice?)

It restores into a temporary directory and changes nothing on the server.
"""


@pytest.fixture
def docs(tmp_path: Path) -> Path:
    root = tmp_path / "docs"
    (root / "sub").mkdir(parents=True)
    (root / "keys").mkdir()
    (root / ".hidden").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "outside.md").write_text("outside the directory")
    (outside / "dir").mkdir()
    (outside / "dir" / "x.md").write_text("inside an outside directory")

    (root / "disaster-recovery.md").write_text(RUNBOOK)
    (root / "README.md").write_text("# Docs\n")
    (root / "notes.TXT").write_text("plain notes\r\nsecond line\r\n")
    (root / "sub" / "guide.md").write_text("a guide")
    (root / "empty.md").write_text("")

    (root / "secrets.md").write_text("where the secrets live")
    (root / "api-token.txt").write_text("named like a token")
    (root / "keys" / "readme.md").write_text("in a directory named like a key")
    (root / "app.py").write_text("def main():\n    return 'ok'\n")
    (root / "Makefile").write_text("up:\n\tdocker compose up -d\n")
    (root / ".env").write_text("TOKEN=abc")
    (root / ".hidden.md").write_text("hidden file")
    (root / ".hidden" / "note.md").write_text("in a hidden directory")
    (root / "config.yml").write_text("a: 1")
    (root / "plain.pem").write_text("-----BEGIN-----")
    (root / "image.md").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00binary")
    (root / "latin1.md").write_bytes("caf\xe9".encode("latin-1"))

    (root / "link-ok.md").symlink_to("README.md")
    (root / "escape.md").symlink_to(outside / "outside.md")
    (root / "dirlink").symlink_to(outside / "dir")
    (root / "to-pem.md").symlink_to("plain.pem")
    (root / "to-env.md").symlink_to(".env")
    (root / "to-sub").symlink_to("sub")
    return root


def read(
    docs: Path, path: str, *, offset: int = 1, limit: int = text_files.DEFAULT_LINES
) -> TextFile:
    return read_text_file({"docs": str(docs)}, "docs", path, offset=offset, limit=limit)


def test_the_runbook_is_read_whole(docs: Path) -> None:
    result = read(docs, "disaster-recovery.md")

    assert result.content == RUNBOOK.rstrip("\n")
    assert "The from-scratch drill" in result.content
    assert (result.directory, result.path) == ("docs", "disaster-recovery.md")
    assert (result.total_lines, result.start_line, result.end_line) == (10, 1, 10)
    assert (result.has_more, result.next_offset, result.notes) == (False, None, [])
    assert result.size_bytes == len(RUNBOOK)
    assert result.modified.endswith("+00:00")


def test_other_text_files_and_subdirectories_can_be_read(docs: Path) -> None:
    assert read(docs, "README.md").content == "# Docs"
    assert read(docs, "sub/guide.md").content == "a guide"
    assert read(docs, "notes.TXT").content == "plain notes\nsecond line"
    assert read(docs, "./sub//guide.md").path == os.path.join("sub", "guide.md")


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("app.py", "def main():\n    return 'ok'"),
        ("Makefile", "up:\n\tdocker compose up -d"),
        ("config.yml", "a: 1"),
        ("plain.pem", "-----BEGIN-----"),
        ("secrets.md", "where the secrets live"),
        ("api-token.txt", "named like a token"),
        ("keys/readme.md", "in a directory named like a key"),
    ],
)
def test_any_visible_text_file_is_read_whatever_its_extension_or_name(
    docs: Path, path: str, expected: str
) -> None:
    assert read(docs, path).content == expected


def test_a_symlink_that_stays_inside_is_followed(docs: Path) -> None:
    result = read(docs, "link-ok.md")

    assert result.content == "# Docs"
    assert result.path == "README.md"
    assert read(docs, "to-sub/guide.md").content == "a guide"


def test_an_empty_file_has_no_lines(docs: Path) -> None:
    result = read(docs, "empty.md")

    assert (result.content, result.total_lines, result.start_line, result.end_line) == ("", 0, 0, 0)
    assert (result.has_more, result.next_offset) == (False, None)


def test_a_byte_order_mark_is_dropped(docs: Path) -> None:
    (docs / "bom.md").write_bytes(b"\xef\xbb\xbf# Title\n")

    assert read(docs, "bom.md").content == "# Title"


def test_a_long_file_comes_in_pages(docs: Path) -> None:
    (docs / "long.md").write_text("".join(f"line {n}\n" for n in range(1, 11)))

    first = read(docs, "long.md", limit=4)
    second = read(docs, "long.md", offset=first.next_offset or 0, limit=4)
    last = read(docs, "long.md", offset=second.next_offset or 0, limit=4)

    assert (first.start_line, first.end_line, first.has_more, first.next_offset) == (1, 4, True, 5)
    assert first.content == "line 1\nline 2\nline 3\nline 4"
    assert "offset=5" in first.notes[0]
    assert (second.start_line, second.end_line, second.next_offset) == (5, 8, 9)
    assert (last.start_line, last.end_line, last.has_more, last.next_offset) == (9, 10, False, None)
    assert last.content == "line 9\nline 10"
    assert all(page.total_lines == 10 for page in (first, second, last))


def test_an_offset_past_the_end_is_an_error(docs: Path) -> None:
    with pytest.raises(TextFileError, match="offset 11 is past the end: the file has 10 lines"):
        read(docs, "disaster-recovery.md", offset=11)


def test_a_page_never_exceeds_its_byte_budget_and_pages_add_up(docs: Path) -> None:
    lines = [f"{n:04d} " + "x" * 995 for n in range(100)]
    (docs / "wide.md").write_text("\n".join(lines))

    pages = []
    offset = 1
    while offset:
        page = read(docs, "wide.md", offset=offset, limit=500)
        assert len(page.content.encode()) <= text_files.MAX_PAGE_BYTES
        pages.append(page)
        offset = page.next_offset or 0

    assert len(pages) > 1
    assert "\n".join(p.content for p in pages) == "\n".join(lines)


def test_a_single_enormous_line_is_cut_and_says_so(docs: Path) -> None:
    (docs / "line.md").write_text("y" * (text_files.MAX_PAGE_BYTES * 2) + "\nnext")

    result = read(docs, "line.md")

    assert len(result.content.encode()) < text_files.MAX_PAGE_BYTES
    assert "A very long line was cut." in result.notes
    assert (result.start_line, result.end_line, result.has_more) == (1, 1, True)
    assert read(docs, "line.md", offset=result.next_offset or 0).content == "next"


def test_credentials_inside_urls_are_masked(docs: Path) -> None:
    (docs / "clone.md").write_text("git clone https://rennan:hunter2@github.com/me/repo.git\n")

    result = read(docs, "clone.md")

    assert "hunter2" not in result.content
    assert "https://rennan:********@github.com/me/repo.git" in result.content
    assert "Credentials found inside URLs were masked." in result.notes


# --- what must never be served --------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("../outside/outside.md", r"must not contain '\.\.'"),
        ("sub/../../outside/outside.md", r"must not contain '\.\.'"),
        ("/etc/passwd", "relative to the directory"),
        ("/docs/README.md", "relative to the directory"),
        ("README.md\0.txt", "invalid character"),
        ("", "must name a file"),
        (".", "must name a file"),
        ("escape.md", "resolves outside"),
        ("dirlink/x.md", "resolves outside"),
        (".env", "Hidden"),
        (".hidden.md", "Hidden"),
        (".hidden/note.md", "Hidden"),
        ("sub/../.env", r"must not contain '\.\.'"),
        ("to-env.md", "Hidden"),
        ("sub", "is a directory"),
        ("image.md", "binary"),
        ("latin1.md", "not valid UTF-8"),
        ("missing.md", "No such file"),
        ("sub/missing.md", "No such file"),
    ],
)
def test_what_must_not_be_served_is_refused(docs: Path, path: str, message: str) -> None:
    with pytest.raises(TextFileError, match=message):
        read(docs, path)


def test_a_directory_named_like_a_text_file_is_not_read(docs: Path) -> None:
    (docs / "folder.md").mkdir()

    with pytest.raises(TextFileError, match="directory"):
        read(docs, "folder.md")


def test_a_refusal_does_not_say_whether_a_hidden_file_exists(docs: Path) -> None:
    with pytest.raises(TextFileError) as existing:
        read(docs, ".env")
    with pytest.raises(TextFileError) as missing:
        read(docs, ".nope")
    with pytest.raises(TextFileError) as inside_hidden_directory:
        read(docs, ".hidden/nope.md")

    assert str(existing.value) == str(missing.value) == str(inside_hidden_directory.value)
    assert "No such file" not in str(existing.value)


def test_a_file_that_is_too_large_is_refused(docs: Path) -> None:
    (docs / "huge.md").write_text("a" * (text_files.MAX_FILE_BYTES + 1))
    (docs / "limit.md").write_text("a" * text_files.MAX_FILE_BYTES)

    with pytest.raises(TextFileError, match="larger than 1024 KiB"):
        read(docs, "huge.md")
    assert read(docs, "limit.md").size_bytes == text_files.MAX_FILE_BYTES


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
def test_an_unreadable_file_is_a_clear_error(docs: Path) -> None:
    locked = docs / "locked.md"
    locked.write_text("nope")
    locked.chmod(0)

    with pytest.raises(TextFileError, match="Permission denied"):
        read(docs, "locked.md")


def test_a_read_error_is_a_clear_error_without_the_path(
    docs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(fd: int) -> os.stat_result:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(text_files.os, "fstat", broken)

    with pytest.raises(TextFileError, match="Cannot read that file: Input/output error") as raised:
        read(docs, "README.md")

    assert str(docs) not in str(raised.value)


def test_unknown_unmounted_and_unconfigured_directories_are_explained(
    docs: Path, tmp_path: Path
) -> None:
    with pytest.raises(TextFileError, match=r"Unknown directory 'nope'; available: docs"):
        read_text_file({"docs": str(docs)}, "nope", "README.md", offset=1, limit=10)
    with pytest.raises(TextFileError, match="not mounted"):
        read_text_file({"docs": str(tmp_path / "gone")}, "docs", "README.md", offset=1, limit=10)
    with pytest.raises(TextFileError, match="SERVER_COPILOT_READABLE_DIRS"):
        read_text_file({}, "docs", "README.md", offset=1, limit=10)


def test_reading_changes_nothing(docs: Path) -> None:
    def snapshot() -> dict[str, tuple[float, int]]:
        return {
            str(p): (p.lstat().st_mtime, p.lstat().st_size)
            for p in sorted(docs.rglob("*"))
            if not p.is_symlink()
        }

    before = snapshot()

    read(docs, "disaster-recovery.md")
    read(docs, "link-ok.md")

    assert snapshot() == before


# --- the tool -------------------------------------------------------------------------------


def make_client() -> DockerClient:
    return DockerClient(
        "tcp://proxy:2375", transport=httpx2.MockTransport(lambda r: httpx2.Response(200))
    )


async def test_the_tool_reads_the_runbook_and_is_registered_read_only(
    docs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SERVER_COPILOT_READABLE_DIRS", f"docs={docs}")
    mcp = create_server(make_client())

    tool = next(t for t in await mcp.list_tools() if t.name == "read_text_file")
    result = await mcp.call_tool(
        "read_text_file", {"directory": "docs", "path": "disaster-recovery.md"}
    )

    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True
    assert tool.description is not None
    assert "never something to run" in tool.description
    assert "docs" in str(tool.input_schema)
    assert isinstance(result, CallToolResult)
    assert result.structured_content is not None
    assert "The from-scratch drill" in result.structured_content["content"]
    assert result.structured_content["total_lines"] == 10


async def test_the_tool_passes_paging_through(docs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVER_COPILOT_READABLE_DIRS", f"docs={docs}")
    mcp = create_server(make_client())

    result = await mcp.call_tool(
        "read_text_file",
        {"directory": "docs", "path": "disaster-recovery.md", "offset": 8, "limit": 1},
    )

    assert isinstance(result, CallToolResult)
    assert result.structured_content is not None
    assert result.structured_content["content"].startswith("### The from-scratch drill")
    assert result.structured_content["next_offset"] == 9


@pytest.mark.parametrize(
    "arguments",
    [
        {"directory": "docs", "path": "README.md", "offset": 0},
        {"directory": "docs", "path": "README.md", "limit": 0},
        {"directory": "docs", "path": "README.md", "limit": text_files.MAX_LINES + 1},
        {"directory": "docs", "path": ""},
        {"directory": "docs", "path": "a" * (text_files.MAX_PATH_LENGTH + 1)},
    ],
)
async def test_the_tool_rejects_out_of_range_arguments(
    docs: Path, monkeypatch: pytest.MonkeyPatch, arguments: dict[str, object]
) -> None:
    monkeypatch.setenv("SERVER_COPILOT_READABLE_DIRS", f"docs={docs}")

    with pytest.raises(ToolError):
        await create_server(make_client()).call_tool("read_text_file", arguments)


async def test_the_tool_turns_refusals_into_tool_errors(
    docs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SERVER_COPILOT_READABLE_DIRS", f"docs={docs}")
    mcp = create_server(make_client())

    for path, message in [(".env", "Hidden"), ("../x.md", "must not contain")]:
        with pytest.raises(ToolError, match=message):
            await mcp.call_tool("read_text_file", {"directory": "docs", "path": path})


async def test_without_configuration_the_tool_explains_how_to_enable_it() -> None:
    with pytest.raises(ToolError, match="SERVER_COPILOT_READABLE_DIRS"):
        await create_server(make_client()).call_tool(
            "read_text_file", {"directory": "docs", "path": "README.md"}
        )


async def test_the_tool_cannot_read_a_host_disk(
    docs: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "containers").mkdir()
    (tmp_path / "containers" / "notes.txt").write_text("app data")
    monkeypatch.setenv("SERVER_COPILOT_HOST_DISKS", f"containers={tmp_path / 'containers'}")
    monkeypatch.setenv("SERVER_COPILOT_READABLE_DIRS", f"docs={docs}")

    with pytest.raises(ToolError, match="Unknown directory 'containers'"):
        await create_server(make_client()).call_tool(
            "read_text_file", {"directory": "containers", "path": "notes.txt"}
        )


async def test_accesses_are_logged_without_their_content(
    docs: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    (docs / "scratch-notes.md").write_text("TOPSECRETMARKER https://u:p4ssw0rd@host/x")
    monkeypatch.setenv("SERVER_COPILOT_READABLE_DIRS", f"docs={docs}")
    caplog.set_level(logging.INFO, logger="uvicorn.error.files")
    mcp = create_server(make_client())

    await mcp.call_tool("read_text_file", {"directory": "docs", "path": "scratch-notes.md"})
    with pytest.raises(ToolError):
        await mcp.call_tool("read_text_file", {"directory": "docs", "path": ".env\nfake"})

    assert "read_text_file directory='docs' path='scratch-notes.md' lines=1-1" in caplog.text
    assert "read_text_file refused: directory='docs' path='.env\\nfake'" in caplog.text
    assert "TOPSECRETMARKER" not in caplog.text
    assert "p4ssw0rd" not in caplog.text
    assert "\nfake" not in caplog.text


# --- listing and configuration --------------------------------------------------------------


async def test_the_readable_directory_can_be_listed_to_find_files(
    docs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SERVER_COPILOT_READABLE_DIRS", f"docs={docs}")
    mcp = create_server(make_client())

    result = await mcp.call_tool("list_files", {"disk": "docs"})

    assert isinstance(result, CallToolResult)
    assert result.structured_content is not None
    names = {e["name"] for e in result.structured_content["entries"]}
    assert {"disaster-recovery.md", "README.md", "sub"} <= names
    assert ".env" not in names


async def test_a_readable_directory_is_not_a_host_disk(
    docs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SERVER_COPILOT_HOST_DISKS", f"data={docs.parent}")
    monkeypatch.setenv("SERVER_COPILOT_READABLE_DIRS", f"docs={docs}")

    with pytest.raises(ToolError, match="Unknown disk 'docs'; available: data"):
        await create_server(make_client()).call_tool("get_disk_usage", {"disk": "docs"})


def test_a_name_cannot_be_both_a_disk_and_a_readable_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SERVER_COPILOT_HOST_DISKS", "docs=/host/docs,data=/host/data")
    monkeypatch.setenv("SERVER_COPILOT_READABLE_DIRS", "docs=/runbooks")

    with pytest.raises(ValueError, match="docs is both a host disk and a readable directory"):
        create_server(make_client())


def test_readable_directory_entries_are_validated_with_their_own_name() -> None:
    assert parse_disks("docs=/runbooks, /notes", "readable directory") == {
        "docs": "/runbooks",
        "/notes": "/notes",
    }
    with pytest.raises(ValueError, match="Invalid readable directory entry 'docs=runbooks'"):
        parse_disks("docs=runbooks", "readable directory")
    with pytest.raises(ValueError, match="Duplicate readable directory name 'docs'"):
        parse_disks("docs=/a,docs=/b", "readable directory")
    with pytest.raises(ValueError, match="Invalid host disk entry"):
        parse_disks("docs=runbooks")
