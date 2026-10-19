from collections import namedtuple
from pathlib import Path

import httpx2
import pytest
from mcp.types import CallToolResult

from server_copilot import host
from server_copilot.docker import DockerClient
from server_copilot.host import collect_host_metrics, parse_disks
from server_copilot.server import create_server

pytestmark = pytest.mark.unit

VirtualMemory = namedtuple("VirtualMemory", "total available percent")
SwapMemory = namedtuple("SwapMemory", "total used percent")
DiskUsageTuple = namedtuple("DiskUsageTuple", "total used free percent")

GIB = 1024**3


@pytest.fixture
def fake_psutil(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host.psutil, "cpu_percent", lambda interval: 37.46)
    monkeypatch.setattr(host.psutil, "cpu_count", lambda logical: 8 if logical else 4)
    monkeypatch.setattr(host.psutil, "getloadavg", lambda: (0.514, 1.0, 1.256))
    monkeypatch.setattr(
        host.psutil, "virtual_memory", lambda: VirtualMemory(16 * GIB, 6 * GIB, 62.5)
    )
    monkeypatch.setattr(host.psutil, "swap_memory", lambda: SwapMemory(4 * GIB, GIB, 25.0))
    monkeypatch.setattr(
        host.psutil, "boot_time", lambda: host.datetime.now(host.UTC).timestamp() - 90061
    )

    def disk_usage(path: str) -> DiskUsageTuple:
        if path == "/host/missing":
            raise FileNotFoundError(2, "No such file or directory", path)
        return DiskUsageTuple(1000 * GIB, 250 * GIB, 750 * GIB, 25.04)

    monkeypatch.setattr(host.psutil, "disk_usage", disk_usage)


def test_parse_disks() -> None:
    assert parse_disks("root=/host/root, data=/host/data ,/mnt/backup") == {
        "root": "/host/root",
        "data": "/host/data",
        "/mnt/backup": "/mnt/backup",
    }


def test_parse_disks_empty() -> None:
    assert parse_disks("") == {}
    assert parse_disks(" , ") == {}


@pytest.mark.parametrize("spec", ["data=relative/path", "=/x", "a=/x,a=/y", "data="])
def test_parse_disks_rejects_invalid_entries(spec: str) -> None:
    with pytest.raises(ValueError, match="host disk"):
        parse_disks(spec)


@pytest.mark.usefixtures("fake_psutil")
def test_collect_host_metrics() -> None:
    metrics = collect_host_metrics({"root": "/host/root", "data": "/host/data"})

    assert metrics.cpu.percent == 37.5
    assert metrics.cpu.logical_cores == 8
    assert metrics.cpu.physical_cores == 4
    assert metrics.cpu.load_average == [0.51, 1.0, 1.26]
    assert metrics.memory.total_bytes == 16 * GIB
    assert metrics.memory.used_bytes == 10 * GIB
    assert metrics.memory.percent == 62.5
    assert metrics.memory.swap_used_bytes == GIB
    assert metrics.uptime_seconds in range(90061, 90065)
    assert [d.name for d in metrics.disks] == ["root", "data"]
    assert metrics.disks[0].used_bytes == 250 * GIB
    assert metrics.disks[0].percent == 25.0
    assert metrics.disk_errors == []
    assert metrics.notes == []
    assert metrics.kernel


@pytest.mark.usefixtures("fake_psutil")
def test_missing_disk_is_reported_not_fatal() -> None:
    metrics = collect_host_metrics({"ok": "/host/data", "gone": "/host/missing"})

    assert [d.name for d in metrics.disks] == ["ok"]
    assert len(metrics.disk_errors) == 1
    assert metrics.disk_errors[0].name == "gone"
    assert "No such file" in metrics.disk_errors[0].error


@pytest.mark.usefixtures("fake_psutil")
def test_note_when_no_disks_configured() -> None:
    metrics = collect_host_metrics({})

    assert metrics.disks == []
    assert "SERVER_COPILOT_HOST_DISKS" in metrics.notes[0]


def test_real_psutil_smoke(tmp_path: Path) -> None:
    try:
        host.psutil.boot_time()
        host.psutil.swap_memory()
    except OSError as exc:
        pytest.skip(f"psutil is blocked in this environment: {exc}")
    metrics = collect_host_metrics({"tmp": str(tmp_path)})

    assert 0 <= metrics.cpu.percent <= 100 * metrics.cpu.logical_cores
    assert metrics.cpu.logical_cores >= 1
    assert metrics.memory.total_bytes > 0
    assert metrics.uptime_seconds > 0
    assert metrics.disks[0].total_bytes > 0
    assert metrics.disks[0].used_bytes + metrics.disks[0].free_bytes <= metrics.disks[0].total_bytes


def make_client() -> DockerClient:
    return DockerClient(
        "tcp://proxy:2375", transport=httpx2.MockTransport(lambda r: httpx2.Response(200))
    )


@pytest.mark.usefixtures("fake_psutil")
async def test_tool_uses_disks_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVER_COPILOT_HOST_DISKS", "data=/host/data")
    mcp = create_server(make_client())

    tool = next(t for t in await mcp.list_tools() if t.name == "get_host_metrics")
    result = await mcp.call_tool("get_host_metrics", {})

    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True
    assert tool.description
    assert isinstance(result, CallToolResult)
    assert result.structured_content is not None
    assert result.structured_content["disks"][0]["name"] == "data"
    assert result.structured_content["cpu"]["logical_cores"] == 8


def test_invalid_disk_setting_fails_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVER_COPILOT_HOST_DISKS", "data=relative")

    with pytest.raises(ValueError, match="host disk"):
        create_server(make_client())
