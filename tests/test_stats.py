import json
from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult

from server_copilot.docker import DockerClient, DockerError
from server_copilot.server import create_server
from server_copilot.stats import compute_stats, fetch_container_stats, fetch_stats_overview

pytestmark = pytest.mark.unit

Handler = Callable[[httpx2.Request], httpx2.Response]

MIB = 1024 * 1024


def v2_payload(
    name: str = "/jellyfin", container_id: str = "a" * 64, cpu: int = 200_000_000
) -> dict[str, Any]:
    return {
        "id": container_id,
        "name": name,
        "cpu_stats": {
            "cpu_usage": {"total_usage": cpu},
            "system_cpu_usage": 10_000_000_000,
            "online_cpus": 4,
        },
        "precpu_stats": {
            "cpu_usage": {"total_usage": 100_000_000},
            "system_cpu_usage": 9_000_000_000,
        },
        "memory_stats": {
            "usage": 500 * MIB,
            "limit": 2048 * MIB,
            "stats": {"inactive_file": 100 * MIB, "anon": 1},
        },
        "networks": {
            "eth0": {"rx_bytes": 1000, "tx_bytes": 500},
            "eth1": {"rx_bytes": 24, "tx_bytes": 12},
        },
        "blkio_stats": {
            "io_service_bytes_recursive": [
                {"major": 8, "minor": 0, "op": "read", "value": 4096},
                {"major": 8, "minor": 0, "op": "write", "value": 1024},
                {"major": 9, "minor": 0, "op": "read", "value": 4096},
            ]
        },
        "pids_stats": {"current": 17},
    }


def make_client(handler: Handler) -> DockerClient:
    return DockerClient("tcp://proxy:2375", transport=httpx2.MockTransport(handler))


def test_compute_stats_cgroup_v2() -> None:
    stats = compute_stats(v2_payload())

    assert stats.id == "a" * 12
    assert stats.name == "jellyfin"
    # (200M-100M) / (10G-9G) * 4 cpus * 100
    assert stats.cpu_percent == 40.0
    assert stats.memory_usage_bytes == 400 * MIB
    assert stats.memory_limit_bytes == 2048 * MIB
    assert stats.memory_percent == 19.53
    assert stats.network_rx_bytes == 1024
    assert stats.network_tx_bytes == 512
    assert stats.block_read_bytes == 8192
    assert stats.block_write_bytes == 1024
    assert stats.pids == 17


def test_compute_stats_cgroup_v1_keys() -> None:
    raw = v2_payload()
    raw["memory_stats"]["stats"] = {"total_inactive_file": 50 * MIB, "cache": 999}
    raw["cpu_stats"].pop("online_cpus")
    raw["cpu_stats"]["cpu_usage"]["percpu_usage"] = [1, 2]
    raw["blkio_stats"]["io_service_bytes_recursive"] = [
        {"op": "Read", "value": 10},
        {"op": "Write", "value": 20},
        {"op": "Total", "value": 30},
    ]

    stats = compute_stats(raw)

    assert stats.memory_usage_bytes == 450 * MIB
    assert stats.cpu_percent == 20.0  # falls back to len(percpu_usage) == 2
    assert (stats.block_read_bytes, stats.block_write_bytes) == (10, 20)


def test_compute_stats_without_previous_sample_or_limits() -> None:
    raw = {
        "id": "b" * 64,
        "name": "/idle",
        "cpu_stats": {"cpu_usage": {"total_usage": 5}, "system_cpu_usage": 100},
        "precpu_stats": {},
        "memory_stats": {},
    }

    stats = compute_stats(raw)

    assert stats.cpu_percent == 0.0
    assert stats.memory_usage_bytes == 0
    assert stats.memory_percent == 0.0
    assert stats.network_rx_bytes == 0
    assert stats.block_read_bytes == 0
    assert stats.pids == 0


def test_cache_larger_than_usage_never_goes_negative() -> None:
    raw = v2_payload()
    raw["memory_stats"]["stats"] = {"inactive_file": 900 * MIB}

    assert compute_stats(raw).memory_usage_bytes == 0


async def test_fetch_container_stats_requests_two_samples() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/containers/app/stats"
        assert request.url.params["stream"] == "false"
        assert "one-shot" not in request.url.params
        return httpx2.Response(200, json=v2_payload())

    async with make_client(handler) as docker:
        assert (await fetch_container_stats(docker, "app")).cpu_percent == 40.0


async def test_fetch_container_stats_rejects_blank_reference() -> None:
    async with make_client(lambda request: httpx2.Response(200)) as docker:
        with pytest.raises(DockerError, match="must not be empty"):
            await fetch_container_stats(docker, " ")


def overview_handler(request: httpx2.Request) -> httpx2.Response:
    path = request.url.path
    if path == "/containers/json":
        assert json.loads(request.url.params["filters"]) == {"status": ["running"]}
        return httpx2.Response(
            200,
            json=[
                {"Id": "1" * 64, "Names": ["/busy"]},
                {"Id": "2" * 64, "Names": ["/hungry"]},
                {"Id": "3" * 64, "Names": ["/gone"]},
                {"Id": "4" * 64, "Names": ["/broken"]},
            ],
        )
    if path == f"/containers/{'1' * 64}/stats":
        return httpx2.Response(200, json=v2_payload("/busy", "1" * 64, cpu=300_000_000))
    if path == f"/containers/{'2' * 64}/stats":
        raw = v2_payload("/hungry", "2" * 64)
        raw["memory_stats"]["usage"] = 1500 * MIB
        return httpx2.Response(200, json=raw)
    if path == f"/containers/{'3' * 64}/stats":
        return httpx2.Response(404, json={"message": "No such container"})
    return httpx2.Response(500, json={"message": "stats unavailable"})


async def test_overview_aggregates_sorts_and_reports_errors() -> None:
    async with make_client(overview_handler) as docker:
        overview = await fetch_stats_overview(docker)

    assert [c.name for c in overview.containers] == ["busy", "hungry"]
    assert overview.total_cpu_percent == 120.0  # 80 + 40
    assert overview.total_memory_usage_bytes == 1400 * MIB + 400 * MIB
    errors = {e.container: e.error for e in overview.errors}
    assert errors["gone"] == "Container disappeared while sampling."
    assert "stats unavailable" in errors["broken"]


async def test_overview_sort_orders() -> None:
    async with make_client(overview_handler) as docker:
        by_memory = await fetch_stats_overview(docker, sort_by="memory")
        by_name = await fetch_stats_overview(docker, sort_by="name")

    assert [c.name for c in by_memory.containers] == ["hungry", "busy"]
    assert [c.name for c in by_name.containers] == ["busy", "hungry"]


async def test_overview_with_no_running_containers() -> None:
    async with make_client(lambda request: httpx2.Response(200, json=[])) as docker:
        overview = await fetch_stats_overview(docker)

    assert overview.containers == []
    assert overview.total_cpu_percent == 0.0
    assert overview.errors == []


async def test_overview_fails_when_container_list_is_blocked() -> None:
    async with make_client(lambda request: httpx2.Response(403)) as docker:
        with pytest.raises(DockerError, match=r"CONTAINERS=1"):
            await fetch_stats_overview(docker)


async def test_tools_are_registered_and_return_structured_results() -> None:
    mcp = create_server(make_client(overview_handler))
    tools = {t.name: t for t in await mcp.list_tools()}

    assert {"get_container_stats", "get_containers_overview"} <= tools.keys()
    assert tools["get_containers_overview"].input_schema["properties"]["sort_by"]["description"]
    assert tools["get_container_stats"].annotations is not None
    assert tools["get_container_stats"].annotations.read_only_hint is True

    single = await mcp.call_tool("get_container_stats", {"container": f"{'1' * 64}"})
    overview = await mcp.call_tool("get_containers_overview", {"sort_by": "name"})

    assert isinstance(single, CallToolResult)
    assert single.structured_content is not None
    assert single.structured_content["cpu_percent"] == 80.0
    assert isinstance(overview, CallToolResult)
    assert overview.structured_content is not None
    assert overview.structured_content["containers"][0]["name"] == "busy"


async def test_tool_surfaces_errors_as_tool_errors() -> None:
    mcp = create_server(make_client(lambda request: httpx2.Response(403)))

    with pytest.raises(ToolError, match=r"CONTAINERS=1"):
        await mcp.call_tool("get_container_stats", {"container": "app"})
    with pytest.raises(ToolError, match=r"CONTAINERS=1"):
        await mcp.call_tool("get_containers_overview", {})


def test_cpu_is_zero_when_usage_did_not_increase() -> None:
    raw = v2_payload(cpu=100_000_000)

    assert compute_stats(raw).cpu_percent == 0.0
