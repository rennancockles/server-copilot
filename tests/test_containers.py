import json
from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult

from server_copilot.containers import fetch_container, fetch_containers
from server_copilot.docker import DockerClient, DockerError, DockerNotFoundError
from server_copilot.masking import MASK
from server_copilot.server import create_server

pytestmark = pytest.mark.unit

Handler = Callable[[httpx2.Request], httpx2.Response]

LIST_PAYLOAD: list[dict[str, Any]] = [
    {
        "Id": "a" * 64,
        "Names": ["/jellyfin"],
        "Image": "jellyfin/jellyfin:10.9",
        "State": "running",
        "Status": "Up 3 days (healthy)",
        "Created": 1760000000,
        "Ports": [
            {"IP": "0.0.0.0", "PrivatePort": 8096, "PublicPort": 8096, "Type": "tcp"},
            {"IP": "::", "PrivatePort": 8096, "PublicPort": 8096, "Type": "tcp"},
            {"PrivatePort": 1900, "Type": "udp"},
        ],
        "Labels": {
            "com.docker.compose.project": "media",
            "com.docker.compose.service": "jellyfin",
        },
    },
    {
        "Id": "b" * 64,
        "Names": ["/old-job"],
        "Image": "busybox",
        "State": "exited",
        "Status": "Exited (1) 2 hours ago",
        "Created": 1760000100,
        "Ports": [],
        "Labels": None,
    },
]

INSPECT_PAYLOAD: dict[str, Any] = {
    "Id": "c" * 64,
    "Name": "/app",
    "Image": "sha256:" + "d" * 64,
    "Created": "2026-10-01T10:00:00.123456789Z",
    "RestartCount": 4,
    "Config": {
        "Image": "ghcr.io/acme/app:1.2",
        "Entrypoint": ["/entrypoint.sh"],
        "Cmd": ["serve"],
        "Env": [
            "PATH=/usr/bin",
            "DB_PASSWORD=hunter2",
            "DATABASE_URL=postgres://admin:s3cr3t@db:5432/app",
            "TZ=America/Sao_Paulo",
        ],
        "Labels": {
            "traefik.http.middlewares.auth.basicauth.users": "admin:$apr1$hash",
            "com.docker.compose.service": "app",
        },
    },
    "State": {
        "Status": "running",
        "Running": True,
        "Paused": False,
        "Restarting": False,
        "OOMKilled": True,
        "ExitCode": 137,
        "Error": "",
        "StartedAt": "2026-10-19T00:00:00Z",
        "FinishedAt": "0001-01-01T00:00:00Z",
        "Health": {
            "Status": "unhealthy",
            "FailingStreak": 3,
            "Log": [
                {"Output": "old"},
                {"Output": "curl: (7) connection refused\n"},
            ],
        },
    },
    "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
    "NetworkSettings": {
        "Ports": {
            "8000/tcp": [{"HostIp": "", "HostPort": "8000"}, {"HostIp": "::", "HostPort": "8000"}],
            "9000/tcp": None,
        },
        "Networks": {
            "server-copilot-net": {
                "IPAddress": "172.20.0.5",
                "Gateway": "172.20.0.1",
                "Aliases": ["app"],
            },
            "none-net": {"IPAddress": "", "Gateway": "", "Aliases": None},
        },
    },
    "Mounts": [
        {"Type": "bind", "Source": "/srv/app", "Destination": "/data", "RW": True},
        {"Type": "volume", "Name": "cache", "Destination": "/cache", "RW": False},
    ],
}


def make_client(handler: Handler) -> DockerClient:
    return DockerClient("tcp://proxy:2375", transport=httpx2.MockTransport(handler))


async def test_list_containers_summarises_payload() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/containers/json"
        assert request.url.params["all"] == "true"
        assert "filters" not in request.url.params
        return httpx2.Response(200, json=LIST_PAYLOAD)

    async with make_client(handler) as docker:
        containers = await fetch_containers(docker)

    jellyfin, old_job = containers
    assert jellyfin.id == "a" * 12
    assert jellyfin.name == "jellyfin"
    assert jellyfin.state == "running"
    assert jellyfin.created == "2025-10-09T08:53:20+00:00"
    assert jellyfin.ports == ["0.0.0.0:8096->8096/tcp", "[::]:8096->8096/tcp", "1900/udp"]
    assert jellyfin.compose_project == "media"
    assert jellyfin.compose_service == "jellyfin"
    assert old_job.compose_project is None
    assert old_job.compose_service is None


async def test_list_containers_sends_filters() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.update(request.url.params)
        return httpx2.Response(200, json=[])

    async with make_client(handler) as docker:
        await fetch_containers(
            docker,
            status="exited",
            name="job",
            label="com.docker.compose.project=media",
            include_stopped=False,
        )

    assert seen["all"] == "true"  # status filter needs stopped containers too
    assert json.loads(seen["filters"]) == {
        "status": ["exited"],
        "name": ["job"],
        "label": ["com.docker.compose.project=media"],
    }


async def test_list_containers_running_only() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.params["all"] == "false"
        return httpx2.Response(200, json=[])

    async with make_client(handler) as docker:
        assert await fetch_containers(docker, include_stopped=False) == []


async def test_inspect_container_extracts_details_and_masks_secrets() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/containers/app/json"
        return httpx2.Response(200, json=INSPECT_PAYLOAD)

    async with make_client(handler) as docker:
        details = await fetch_container(docker, "app")

    assert details.id == "c" * 12
    assert details.name == "app"
    assert details.image == "ghcr.io/acme/app:1.2"
    assert details.restart_count == 4
    assert details.restart_policy == "unless-stopped"
    assert details.state.oom_killed is True
    assert details.state.exit_code == 137
    assert details.state.error is None
    assert details.health is not None
    assert details.health.status == "unhealthy"
    assert details.health.failing_streak == 3
    assert details.health.last_output == "curl: (7) connection refused"
    assert details.ports == ["0.0.0.0:8000->8000/tcp", "[::]:8000->8000/tcp", "9000/tcp"]
    assert [n.name for n in details.networks] == ["server-copilot-net", "none-net"]
    assert details.networks[0].ip_address == "172.20.0.5"
    assert details.networks[1].ip_address is None
    assert [(m.type, m.source, m.read_only) for m in details.mounts] == [
        ("bind", "/srv/app", False),
        ("volume", "cache", True),
    ]
    env = {item.name: item.value for item in details.env}
    assert env == {
        "PATH": "/usr/bin",
        "DB_PASSWORD": MASK,
        "DATABASE_URL": f"postgres://admin:{MASK}@db:5432/app",
        "TZ": "America/Sao_Paulo",
    }
    assert details.labels == {
        "traefik.http.middlewares.auth.basicauth.users": MASK,
        "com.docker.compose.service": "app",
    }
    assert "hunter2" not in details.model_dump_json()
    assert "s3cr3t" not in details.model_dump_json()


async def test_inspect_container_without_health_or_networks() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"Id": "e" * 64, "Name": "/bare", "State": None})

    async with make_client(handler) as docker:
        details = await fetch_container(docker, "bare")

    assert details.health is None
    assert details.networks == []
    assert details.mounts == []
    assert details.env == []


async def test_inspect_container_escapes_reference() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.raw_path == b"/containers/..%2Fimages%2Fjson/json"
        return httpx2.Response(404, json={"message": "No such container"})

    async with make_client(handler) as docker:
        with pytest.raises(DockerNotFoundError):
            await fetch_container(docker, "../images/json")


async def test_inspect_container_rejects_blank_reference() -> None:
    async with make_client(lambda request: httpx2.Response(200)) as docker:
        with pytest.raises(DockerError, match="must not be empty"):
            await fetch_container(docker, "  ")


async def test_tools_are_registered_with_schemas() -> None:
    mcp = create_server(make_client(lambda request: httpx2.Response(200, json=[])))
    tools = {tool.name: tool for tool in await mcp.list_tools()}

    assert {"list_containers", "inspect_container"} <= tools.keys()
    list_tool = tools["list_containers"]
    assert list_tool.description
    properties = list_tool.input_schema["properties"]
    assert set(properties) == {"status", "name", "label", "include_stopped"}
    assert all(prop.get("description") for prop in properties.values())
    inspect_tool = tools["inspect_container"]
    assert inspect_tool.input_schema["required"] == ["container"]
    assert inspect_tool.annotations is not None
    assert inspect_tool.annotations.read_only_hint is True


async def test_list_containers_tool_returns_structured_result() -> None:
    mcp = create_server(make_client(lambda request: httpx2.Response(200, json=LIST_PAYLOAD)))

    result = await mcp.call_tool("list_containers", {"status": "running"})

    assert isinstance(result, CallToolResult)
    assert result.structured_content is not None
    assert result.structured_content["result"][0]["name"] == "jellyfin"


async def test_tool_surfaces_docker_errors_as_tool_errors() -> None:
    mcp = create_server(make_client(lambda request: httpx2.Response(403)))

    with pytest.raises(ToolError, match=r"Enable CONTAINERS=1"):
        await mcp.call_tool("list_containers", {})


async def test_inspect_tool_surfaces_not_found_as_tool_error() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(404, json={"message": "No such container: ghost"})

    mcp = create_server(make_client(handler))

    with pytest.raises(ToolError, match="No such container: ghost"):
        await mcp.call_tool("inspect_container", {"container": "ghost"})


async def test_inspect_tool_returns_masked_structured_result() -> None:
    mcp = create_server(make_client(lambda request: httpx2.Response(200, json=INSPECT_PAYLOAD)))

    result = await mcp.call_tool("inspect_container", {"container": "app"})

    assert isinstance(result, CallToolResult)
    assert result.structured_content is not None
    assert result.structured_content["restart_count"] == 4
    assert "hunter2" not in json.dumps(result.structured_content)
