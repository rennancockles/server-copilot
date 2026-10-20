import asyncio
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import httpx2
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult

from server_copilot import disk_usage
from server_copilot.disk_usage import DiskUsageError, measure_disk_usage, resolve_target
from server_copilot.docker import DockerClient
from server_copilot.server import create_server

pytestmark = pytest.mark.unit

KIB = 1024


def make_client() -> DockerClient:
    return DockerClient(
        "tcp://proxy:2375", transport=httpx2.MockTransport(lambda r: httpx2.Response(200))
    )


@pytest.fixture
def home(tmp_path: Path) -> Path:
    root = Path(os.path.realpath(tmp_path)) / "home"
    (root / "Containers" / "plex").mkdir(parents=True)
    (root / "other").mkdir()
    (root / "big.iso").write_bytes(b"x")
    return root


@pytest.fixture
def fake_du(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    args_file = tmp_path / "du-args.txt"
    nice_file = tmp_path / "nice-args.txt"
    # A stand-in for nice: the real one warns on stderr where priorities cannot be changed.
    nice = bin_dir / "nice"
    nice.write_text(f'#!/bin/sh\necho "$1 $2" > \'{nice_file}\'\nshift 2\nexec "$@"\n')
    nice.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    def install(stdout: str = "", stderr: str = "", exit_code: int = 0, sleep: float = 0.0) -> Path:
        ending = f"exec sleep {sleep}" if sleep else f"exit {exit_code}"
        script = bin_dir / "du"
        script.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$@\" > '{args_file}'\n"
            f"cat <<'OUT'\n{stdout}OUT\n"
            f"cat >&2 <<'ERR'\n{stderr}ERR\n"
            f"{ending}\n"
        )
        script.chmod(0o755)
        return args_file

    return install


def line(size: int, path: Path) -> str:
    return f"{size}\t{path}\n"


def test_resolve_target_accepts_the_root_and_nested_directories(home: Path) -> None:
    disks = {"home": str(home)}

    assert resolve_target(disks, "home", "") == (str(home), str(home))
    assert resolve_target(disks, "home", "Containers/plex") == (
        str(home),
        str(home / "Containers" / "plex"),
    )


def test_resolve_target_explains_missing_configuration() -> None:
    with pytest.raises(DiskUsageError, match="SERVER_COPILOT_HOST_DISKS"):
        resolve_target({}, "home", "")


def test_resolve_target_lists_the_available_disks(home: Path) -> None:
    with pytest.raises(DiskUsageError, match="available: data, home"):
        resolve_target({"home": str(home), "data": str(home)}, "nope", "")


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("/etc", "relative"),
        ("/home/rennan", "relative"),
        ("../etc", r"'\.\.'"),
        ("Containers/../../etc", r"'\.\.'"),
        ("a\0b", "invalid character"),
    ],
)
def test_resolve_target_rejects_unsafe_paths(home: Path, path: str, message: str) -> None:
    with pytest.raises(DiskUsageError, match=message):
        resolve_target({"home": str(home)}, "home", path)


def test_resolve_target_refuses_symlinks_that_leave_the_disk(home: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / "escape").symlink_to(outside)

    with pytest.raises(DiskUsageError, match="outside the disk"):
        resolve_target({"home": str(home)}, "home", "escape")


def test_resolve_target_follows_symlinks_that_stay_inside(home: Path) -> None:
    (home / "shortcut").symlink_to(home / "Containers")

    assert resolve_target({"home": str(home)}, "home", "shortcut")[1] == str(home / "Containers")


def test_resolve_target_requires_an_existing_directory(home: Path) -> None:
    with pytest.raises(DiskUsageError, match="not a directory"):
        resolve_target({"home": str(home)}, "home", "missing")
    with pytest.raises(DiskUsageError, match="not a directory"):
        resolve_target({"home": str(home)}, "home", "big.iso")


def test_resolve_target_reports_an_unmounted_disk(tmp_path: Path) -> None:
    with pytest.raises(DiskUsageError, match="not mounted"):
        resolve_target({"gone": str(tmp_path / "missing")}, "gone", "")


async def test_largest_entries_come_first_with_shares(
    home: Path, fake_du: Callable[..., Path]
) -> None:
    args_file = fake_du(
        stdout=(
            line(50, home / "other")
            + line(600, home / "Containers")
            + line(300, home / "big.iso")
            + line(1000, home)
        )
    )

    report = await measure_disk_usage({"home": str(home)}, "home", limit=2)

    assert report.total_bytes == 1000
    assert [(e.path, e.size_bytes, e.percent_of_total, e.kind) for e in report.entries] == [
        ("Containers", 600, 60.0, "directory"),
        ("big.iso", 300, 30.0, "file"),
    ]
    assert report.returned == 2
    assert report.omitted == 1
    assert report.complete is True
    assert report.path == "."
    assert any("2 largest of 3" in note for note in report.notes)
    assert (args_file.parent / "nice-args.txt").read_text().strip() == "-n 10"
    assert args_file.read_text().split() == [
        "--block-size=1",
        "--no-dereference",
        "--max-depth=1",
        "--all",
        "--one-file-system",
        "--",
        str(home),
    ]


async def test_options_change_the_du_arguments(home: Path, fake_du: Callable[..., Path]) -> None:
    args_file = fake_du(stdout=line(10, home))

    await measure_disk_usage(
        {"home": str(home)}, "home", max_depth=3, include_files=False, cross_filesystems=True
    )

    args = args_file.read_text().split()
    assert "--max-depth=3" in args
    assert "--all" not in args
    assert "--one-file-system" not in args


async def test_nested_path_reports_paths_relative_to_the_disk(
    home: Path, fake_du: Callable[..., Path]
) -> None:
    fake_du(stdout=line(400, home / "Containers" / "plex") + line(450, home / "Containers"))

    report = await measure_disk_usage({"home": str(home)}, "home", "Containers")

    assert report.path == "Containers"
    assert report.total_bytes == 450
    assert [e.path for e in report.entries] == ["Containers/plex"]
    assert report.omitted == 0
    assert report.notes == []


async def test_unreadable_entries_are_counted_and_paths_are_trimmed(
    home: Path, fake_du: Callable[..., Path]
) -> None:
    fake_du(
        stdout=line(70, home / "other") + line(100, home),
        stderr=(
            f"du: cannot read directory '{home}/secret': Permission denied\n"
            f"du: cannot access '{home}/gone': No such file or directory\n"
        ),
        exit_code=1,
    )

    report = await measure_disk_usage({"home": str(home)}, "home")

    assert report.complete is True
    assert report.unreadable == 2
    note = next(n for n in report.notes if "could not be read" in n)
    assert "2 entries" in note
    assert "secret" in note
    assert str(home) not in note


async def test_a_du_failure_is_reported(home: Path, fake_du: Callable[..., Path]) -> None:
    fake_du(stderr="du: unrecognized option '--block-size=1'\n", exit_code=2)

    with pytest.raises(DiskUsageError, match="du failed: du: unrecognized option"):
        await measure_disk_usage({"home": str(home)}, "home")


async def test_timeout_returns_what_finished_and_stops_du(
    home: Path, fake_du: Callable[..., Path]
) -> None:
    fake_du(stdout=line(300, home / "Containers"), sleep=30)

    report = await measure_disk_usage({"home": str(home)}, "home", time_limit=2)

    assert report.complete is False
    assert report.total_bytes is None
    assert [e.path for e in report.entries] == ["Containers"]
    assert report.entries[0].percent_of_total is None
    assert report.elapsed_seconds < 10
    assert any("Stopped after 2s" in note for note in report.notes)


async def test_missing_du_is_reported(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    with pytest.raises(DiskUsageError, match="du is not available"):
        await measure_disk_usage({"home": str(home)}, "home")


def has_gnu_du() -> bool:
    try:
        result = subprocess.run(["du", "--version"], capture_output=True, text=True, check=False)
    except OSError:
        return False
    return "GNU" in result.stdout


@pytest.mark.skipif(not has_gnu_du(), reason="needs GNU du (the container image has it)")
async def test_real_du_ranks_directories_and_does_not_follow_symlinks(
    home: Path, tmp_path: Path
) -> None:
    (home / "a").mkdir()
    for index in range(3):
        (home / "a" / f"file{index}").write_bytes(b"x" * 64 * KIB)
    (home / "b").mkdir()
    (home / "b" / "file").write_bytes(b"x" * 8 * KIB)
    (home / "c.bin").write_bytes(b"x" * 128 * KIB)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "huge").write_bytes(b"x" * 1024 * KIB)
    (home / "link").symlink_to(outside)

    report = await measure_disk_usage({"home": str(home)}, "home", limit=4)

    names = [e.path for e in report.entries]
    assert names[:3] == ["a", "c.bin", "b"]
    link = next(e for e in report.entries if e.path == "link")
    assert link.kind == "file"
    assert link.size_bytes < 8 * KIB
    assert report.total_bytes is not None
    assert report.total_bytes >= sum(e.size_bytes for e in report.entries[:3])
    assert report.complete is True


async def test_tool_is_registered_with_bounded_arguments(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SERVER_COPILOT_HOST_DISKS", f"home={home},data={home}")
    mcp = create_server(make_client())

    tool = next(t for t in await mcp.list_tools() if t.name == "get_disk_usage")

    properties = tool.input_schema["properties"]
    assert set(properties) == {
        "disk",
        "path",
        "max_depth",
        "limit",
        "include_files",
        "cross_filesystems",
        "timeout_seconds",
    }
    assert all(prop.get("description") for prop in properties.values())
    assert "data, home" in properties["disk"]["description"]
    assert properties["max_depth"]["maximum"] == disk_usage.MAX_DEPTH
    assert properties["limit"]["maximum"] == disk_usage.MAX_LIMIT
    assert properties["timeout_seconds"]["maximum"] == disk_usage.MAX_TIMEOUT
    assert tool.input_schema["required"] == ["disk"]
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True


async def test_tool_returns_a_structured_report(
    home: Path, fake_du: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SERVER_COPILOT_HOST_DISKS", f"home={home}")
    fake_du(stdout=line(600, home / "Containers") + line(700, home))
    mcp = create_server(make_client())

    result = await mcp.call_tool("get_disk_usage", {"disk": "home"})

    assert isinstance(result, CallToolResult)
    assert result.structured_content is not None
    assert result.structured_content["entries"][0]["path"] == "Containers"
    assert result.structured_content["total_bytes"] == 700


async def test_tool_surfaces_errors_as_tool_errors(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SERVER_COPILOT_HOST_DISKS", raising=False)
    mcp = create_server(make_client())

    with pytest.raises(ToolError, match="SERVER_COPILOT_HOST_DISKS"):
        await mcp.call_tool("get_disk_usage", {"disk": "home"})

    monkeypatch.setenv("SERVER_COPILOT_HOST_DISKS", f"home={home}")
    mcp = create_server(make_client())
    with pytest.raises(ToolError, match="relative"):
        await mcp.call_tool("get_disk_usage", {"disk": "home", "path": "/etc"})
    with pytest.raises(ToolError):
        await mcp.call_tool("get_disk_usage", {"disk": "home", "max_depth": 99})


async def test_only_one_scan_runs_at_a_time(
    home: Path, fake_du: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SERVER_COPILOT_HOST_DISKS", f"home={home}")
    fake_du(stdout=line(5, home / "other"), sleep=2)
    mcp = create_server(make_client())

    results = await asyncio.gather(
        mcp.call_tool("get_disk_usage", {"disk": "home", "timeout_seconds": 5}),
        mcp.call_tool("get_disk_usage", {"disk": "home", "timeout_seconds": 5}),
        return_exceptions=True,
    )

    rejected = [r for r in results if isinstance(r, ToolError)]
    assert len(rejected) == 1
    assert "Another disk scan" in str(rejected[0])


async def test_other_stderr_output_is_not_counted_as_unreadable(
    home: Path, fake_du: Callable[..., Path]
) -> None:
    fake_du(stdout=line(100, home), stderr="nice: setpriority: Operation not permitted\n")

    report = await measure_disk_usage({"home": str(home)}, "home")

    assert report.unreadable == 0
    assert report.notes == []


async def test_malformed_du_lines_are_skipped(home: Path, fake_du: Callable[..., Path]) -> None:
    fake_du(stdout="garbage\n\tnoname\n" + line(70, home / "other") + line(100, home))

    report = await measure_disk_usage({"home": str(home)}, "home")

    assert [e.path for e in report.entries] == ["other"]


async def test_failure_without_du_messages_uses_the_other_stderr(
    home: Path, fake_du: Callable[..., Path]
) -> None:
    fake_du(stderr="nice: cannot run du\n", exit_code=2)

    with pytest.raises(DiskUsageError, match="du failed: nice: cannot run du"):
        await measure_disk_usage({"home": str(home)}, "home")


async def test_failure_with_no_output_reports_the_exit_code(
    home: Path, fake_du: Callable[..., Path]
) -> None:
    fake_du(exit_code=3)

    with pytest.raises(DiskUsageError, match="exit code 3"):
        await measure_disk_usage({"home": str(home)}, "home")
