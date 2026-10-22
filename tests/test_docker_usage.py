from typing import Any

import httpx2
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult

from server_copilot.docker import DockerClient
from server_copilot.docker_usage import build_report, fetch_docker_disk_usage
from server_copilot.server import create_server

pytestmark = pytest.mark.unit

MIB = 1024 * 1024


def df_payload() -> dict[str, Any]:
    return {
        "LayersSize": 900 * MIB,
        "Images": [
            {
                "Id": "sha256:" + "a" * 64,
                "RepoTags": ["plex:1.40"],
                "Size": 400 * MIB,
                "SharedSize": 100 * MIB,
                "Containers": 1,
            },
            {
                "Id": "sha256:" + "b" * 64,
                "RepoTags": ["postgres:14"],
                "Size": 300 * MIB,
                "SharedSize": 100 * MIB,
                "Containers": 0,
            },
            {
                "Id": "sha256:" + "c" * 64,
                "RepoTags": ["<none>:<none>"],
                "Size": 200 * MIB,
                "SharedSize": -1,
                "Containers": 0,
            },
            {"Id": "sha256:" + "d" * 64, "RepoTags": None, "Size": 50, "Containers": -1},
        ],
        "Containers": [
            {"Id": "1" * 64, "Names": ["/plex"], "State": "running", "SizeRw": 10 * MIB},
            {
                "Id": "2" * 64,
                "Names": ["/old-job"],
                "State": "exited",
                "Status": "Exited (0) 3 days ago",
                "SizeRw": 5 * MIB,
            },
        ],
        "Volumes": [
            {
                "Name": "pgdata",
                "Mountpoint": "/var/lib/docker/volumes/pgdata/_data",
                "Labels": {"secret": "x"},
                "UsageData": {"Size": 800 * MIB, "RefCount": 0},
            },
            {"Name": "plexdata", "UsageData": {"Size": 50 * MIB, "RefCount": 1}},
            {"Name": "mystery", "UsageData": {"Size": -1, "RefCount": 0}},
        ],
        "BuildCache": [
            {"ID": "e" * 40, "Type": "regular", "InUse": False, "Size": 120 * MIB},
            {"ID": "f" * 40, "Type": "regular", "InUse": True, "Size": 30 * MIB},
        ],
    }


def make_client(payload: Any, status: int = 200) -> DockerClient:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/v1.44/system/df"
        return httpx2.Response(status, json=payload)

    return DockerClient("tcp://proxy:2375", transport=httpx2.MockTransport(handler))


def test_summaries_count_totals_and_reclaimable() -> None:
    report = build_report(df_payload())

    assert report.images.count == 4
    assert report.images.size_bytes == 900 * MIB
    # postgres: 300-100 unique; untagged: 200 (shared size unknown); id d has Containers=-1.
    assert report.images.reclaimable_bytes == 400 * MIB
    assert report.containers.reclaimable_bytes == 5 * MIB
    assert report.volumes.size_bytes == 850 * MIB
    assert report.volumes.reclaimable_bytes == 800 * MIB
    assert report.build_cache.size_bytes == 150 * MIB
    assert report.build_cache.reclaimable_bytes == 120 * MIB
    assert report.total_bytes == (900 + 15 + 850 + 150) * MIB
    assert report.reclaimable_bytes == (400 + 5 + 800 + 120) * MIB


def test_candidates_are_sorted_largest_first_and_explained() -> None:
    report = build_report(df_payload())

    names = [c.name for c in report.cleanup_candidates]
    assert names == ["pgdata", "postgres:14", "cccccccccccc", "e" * 12, "old-job", "mystery"]
    pgdata = report.cleanup_candidates[0]
    assert pgdata.kind == "volume"
    assert "check before removing" in pgdata.reason
    untagged = report.cleanup_candidates[2]
    assert untagged.reason == "untagged and not used by any container"
    assert report.cleanup_candidates[-1].size_bytes is None
    assert report.notes == ["1 unused volume(s) have an unknown size and count as 0 reclaimable."]


def test_in_use_things_are_never_candidates() -> None:
    names = {c.name for c in build_report(df_payload()).cleanup_candidates}

    assert names.isdisjoint({"plex:1.40", "plex", "plexdata", "f" * 12})


def test_top_limits_candidates_and_reports_the_rest() -> None:
    report = build_report(df_payload(), top=2)

    assert [c.name for c in report.cleanup_candidates] == ["pgdata", "postgres:14"]
    assert report.candidates_not_shown == 4


def test_sensitive_volume_details_are_not_exposed() -> None:
    dumped = build_report(df_payload()).model_dump_json()

    assert "Mountpoint" not in dumped
    assert "/var/lib/docker" not in dumped
    assert "secret" not in dumped


def test_empty_docker_gives_an_empty_report() -> None:
    report = build_report({"Images": None, "Containers": None, "Volumes": None, "BuildCache": None})

    assert report.total_bytes == 0
    assert report.cleanup_candidates == []
    assert report.notes == []


def test_image_total_falls_back_to_summed_sizes_without_layers_size() -> None:
    payload = df_payload()
    del payload["LayersSize"]

    assert build_report(payload).images.size_bytes == 900 * MIB + 50


async def test_fetch_uses_the_pinned_endpoint() -> None:
    async with make_client(df_payload()) as docker:
        report = await fetch_docker_disk_usage(docker)

    assert report.reclaimable_bytes > 0


async def test_tool_is_registered_read_only_with_bounded_top() -> None:
    mcp = create_server(make_client(df_payload()))
    tool = next(t for t in await mcp.list_tools() if t.name == "get_docker_disk_usage")

    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True
    top = tool.input_schema["properties"]["top"]
    assert (top["minimum"], top["maximum"]) == (1, 100)


async def test_tool_returns_a_structured_report() -> None:
    mcp = create_server(make_client(df_payload()))

    result = await mcp.call_tool("get_docker_disk_usage", {"top": 3})

    assert isinstance(result, CallToolResult)
    assert result.structured_content is not None
    assert len(result.structured_content["cleanup_candidates"]) == 3
    assert result.structured_content["candidates_not_shown"] == 3


async def test_tool_names_the_proxy_variable_when_blocked() -> None:
    mcp = create_server(make_client({}, status=403))

    with pytest.raises(ToolError, match=r"SYSTEM=1"):
        await mcp.call_tool("get_docker_disk_usage", {})
