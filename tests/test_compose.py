import json
from typing import Any

import httpx2
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult

from server_copilot.compose import build_projects, fetch_compose_projects
from server_copilot.docker import DockerClient
from server_copilot.masking import MASK
from server_copilot.server import create_server

pytestmark = pytest.mark.unit


ONEOFF = {"com.docker.compose.oneoff": "True"}


def container(
    name: str,
    project: str | None,
    service: str | None,
    *,
    state: str = "running",
    extra: dict[str, str] | None = None,
) -> dict[str, Any]:
    labels: dict[str, str] = {}
    if project:
        labels["com.docker.compose.project"] = project
        labels["com.docker.compose.project.working_dir"] = f"/srv/{project}"
        labels["com.docker.compose.project.config_files"] = (
            f"/srv/{project}/compose.yml,/srv/{project}/compose.prod.yml"
        )
        labels["com.docker.compose.oneoff"] = "False"
    if service:
        labels["com.docker.compose.service"] = service
    labels.update(extra or {})
    return {
        "Id": name * 20,
        "Names": [f"/{project}-{service}-1" if project else f"/{name}"],
        "Image": "img:1",
        "State": state,
        "Status": "Up 2 hours" if state == "running" else "Exited (0) 1 hour ago",
        "Labels": labels,
    }


def containers() -> list[dict[str, Any]]:
    return [
        container(
            "a", "media", "plex", extra={"traefik.http.routers.plex.rule": "Host(`p.example`)"}
        ),
        container("b", "media", "sonarr", state="exited"),
        container("c", "media", "sonarr"),
        container("d", "finsight", "db"),
        container("e", "finsight", "api", state="exited"),
        container("f", None, None),
    ]


def test_groups_containers_by_project_and_service() -> None:
    projects = build_projects(containers(), include_labels=False).projects

    assert [p.name for p in projects] == ["finsight", "media"]
    media = projects[1]
    assert [s.name for s in media.services] == ["plex", "sonarr"]
    assert media.container_count == 3
    assert media.running_count == 2
    sonarr = media.services[1]
    assert (len(sonarr.containers), sonarr.running_count) == (2, 1)
    assert sonarr.image == "img:1"


def test_paths_come_from_the_compose_labels() -> None:
    media = build_projects(containers(), include_labels=False).projects[1]

    assert media.working_dir == "/srv/media"
    assert media.config_files == ["/srv/media/compose.yml", "/srv/media/compose.prod.yml"]


def test_status_is_running_partial_or_stopped() -> None:
    by_name = {
        p.name: p.status for p in build_projects(containers(), include_labels=False).projects
    }
    assert by_name == {"finsight": "partial", "media": "partial"}

    up = [container("a", "x", "web")]
    down = [container("a", "x", "web", state="exited")]
    assert build_projects(up, include_labels=False).projects[0].status == "running"
    assert build_projects(down, include_labels=False).projects[0].status == "stopped"


def test_leftover_one_off_containers_do_not_make_a_project_partial() -> None:
    items = [
        container("a", "x", "web"),
        container("b", "x", "web", state="exited", extra=ONEOFF),
    ]

    project = build_projects(items, include_labels=False).projects[0]

    assert project.status == "running"
    assert project.container_count == 2
    assert any(c.oneoff for c in project.services[0].containers)


def test_labels_are_opt_in_masked_and_exclude_compose_labels() -> None:
    items = [
        container(
            "a",
            "x",
            "web",
            extra={
                "traefik.http.routers.web.rule": "Host(`w.example`)",
                "traefik.http.middlewares.auth.basicauth.users": "admin:$apr1$hash",
                "wud.watch": "true",
            },
        )
    ]

    hidden = build_projects(items, include_labels=False).projects[0].services[0]
    shown = build_projects(items, include_labels=True).projects[0].services[0]

    assert hidden.labels == {}
    assert shown.labels["traefik.http.routers.web.rule"] == "Host(`w.example`)"
    assert shown.labels["traefik.http.middlewares.auth.basicauth.users"] == MASK
    assert shown.labels["wud.watch"] == "true"
    assert not any(key.startswith("com.docker.compose.") for key in shown.labels)


def test_a_container_without_a_service_label_is_still_listed() -> None:
    project = build_projects([container("a", "x", None)], include_labels=False).projects[0]

    assert project.services[0].name == "(unknown)"


def test_no_compose_containers_gives_an_empty_inventory() -> None:
    assert build_projects([container("a", None, None)], include_labels=False).projects == []
    assert build_projects([], include_labels=False).projects == []


def test_a_project_without_path_labels_has_no_working_dir() -> None:
    item = container("a", "x", "web")
    del item["Labels"]["com.docker.compose.project.working_dir"]
    del item["Labels"]["com.docker.compose.project.config_files"]

    project = build_projects([item], include_labels=False).projects[0]

    assert project.working_dir is None
    assert project.config_files == []


def make_client(handler: Any) -> DockerClient:
    return DockerClient("tcp://proxy:2375", transport=httpx2.MockTransport(handler))


def listing(request: httpx2.Request) -> httpx2.Response:
    assert request.url.path == "/containers/json"
    assert request.url.params["all"] == "true"
    wanted = json.loads(request.url.params["filters"])["label"][0]
    name = wanted.partition("=")[2]
    items = [
        c
        for c in containers()
        if "com.docker.compose.project" in c["Labels"]
        and (not name or c["Labels"]["com.docker.compose.project"] == name)
    ]
    return httpx2.Response(200, json=items)


async def test_fetch_filters_by_the_project_label() -> None:
    async with make_client(listing) as docker:
        everything = await fetch_compose_projects(docker)
        one = await fetch_compose_projects(docker, project="media")

    assert [p.name for p in everything.projects] == ["finsight", "media"]
    assert [p.name for p in one.projects] == ["media"]


async def test_tool_is_registered_read_only_and_returns_structured_content() -> None:
    mcp = create_server(make_client(listing))
    tool = next(t for t in await mcp.list_tools() if t.name == "get_compose_projects")

    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True

    result = await mcp.call_tool("get_compose_projects", {"project": "finsight"})

    assert isinstance(result, CallToolResult)
    assert result.structured_content is not None
    assert result.structured_content["projects"][0]["name"] == "finsight"


async def test_tool_surfaces_docker_errors_as_tool_errors() -> None:
    mcp = create_server(make_client(lambda request: httpx2.Response(403)))

    with pytest.raises(ToolError, match=r"CONTAINERS=1"):
        await mcp.call_tool("get_compose_projects", {})
