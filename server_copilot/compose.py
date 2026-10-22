import json
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from server_copilot.containers import (
    COMPOSE_PROJECT_LABEL,
    COMPOSE_SERVICE_LABEL,
    SHORT_ID_LENGTH,
)
from server_copilot.docker import DockerClient, DockerError
from server_copilot.masking import mask_value

COMPOSE_LABEL_PREFIX = "com.docker.compose."
WORKING_DIR_LABEL = "com.docker.compose.project.working_dir"
CONFIG_FILES_LABEL = "com.docker.compose.project.config_files"
ONEOFF_LABEL = "com.docker.compose.oneoff"
UNKNOWN_SERVICE = "(unknown)"

ProjectStatus = Literal["running", "partial", "stopped"]


class ComposeContainer(BaseModel):
    id: str
    name: str
    state: str
    status: str
    oneoff: bool


class ComposeService(BaseModel):
    name: str
    image: str
    running_count: int
    containers: list[ComposeContainer]
    labels: dict[str, str]


class ComposeProject(BaseModel):
    name: str
    status: ProjectStatus
    working_dir: str | None
    config_files: list[str]
    container_count: int
    running_count: int
    services: list[ComposeService]


class ComposeProjects(BaseModel):
    projects: list[ComposeProject]


def _container(raw: dict[str, Any]) -> ComposeContainer:
    labels = raw.get("Labels") or {}
    return ComposeContainer(
        id=raw.get("Id", "")[:SHORT_ID_LENGTH],
        name=(raw.get("Names") or [raw.get("Id", "")])[0].lstrip("/"),
        state=raw.get("State", ""),
        status=raw.get("Status", ""),
        oneoff=str(labels.get(ONEOFF_LABEL, "")).lower() == "true",
    )


def _service_labels(raw: dict[str, Any]) -> dict[str, str]:
    return {
        key: mask_value(key, value)
        for key, value in (raw.get("Labels") or {}).items()
        if not key.startswith(COMPOSE_LABEL_PREFIX)
    }


def _project_status(containers: list[ComposeContainer]) -> ProjectStatus:
    # `docker compose run` leaves one-off containers behind; they must not make a project look
    # partially down.
    considered = [c for c in containers if not c.oneoff] or containers
    running = sum(c.state == "running" for c in considered)
    if running == len(considered):
        return "running"
    return "partial" if running else "stopped"


def _service(name: str, raws: list[dict[str, Any]], *, include_labels: bool) -> ComposeService:
    containers = sorted((_container(raw) for raw in raws), key=lambda c: c.name)
    return ComposeService(
        name=name,
        image=raws[0].get("Image", ""),
        running_count=sum(c.state == "running" for c in containers),
        containers=containers,
        labels=_service_labels(raws[0]) if include_labels else {},
    )


def _project(
    name: str, services: dict[str, list[dict[str, Any]]], *, include_labels: bool
) -> ComposeProject:
    first_labels = next(iter(services.values()))[0].get("Labels") or {}
    config_files = [f for f in first_labels.get(CONFIG_FILES_LABEL, "").split(",") if f]
    built = [
        _service(service, raws, include_labels=include_labels)
        for service, raws in sorted(services.items())
    ]
    containers = [c for service in built for c in service.containers]
    return ComposeProject(
        name=name,
        status=_project_status(containers),
        working_dir=first_labels.get(WORKING_DIR_LABEL) or None,
        config_files=config_files,
        container_count=len(containers),
        running_count=sum(service.running_count for service in built),
        services=built,
    )


def build_projects(containers: list[dict[str, Any]], *, include_labels: bool) -> ComposeProjects:
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for raw in containers:
        labels = raw.get("Labels") or {}
        if project := labels.get(COMPOSE_PROJECT_LABEL):
            service = labels.get(COMPOSE_SERVICE_LABEL) or UNKNOWN_SERVICE
            grouped.setdefault(project, {}).setdefault(service, []).append(raw)
    return ComposeProjects(
        projects=[
            _project(name, services, include_labels=include_labels)
            for name, services in sorted(grouped.items())
        ]
    )


async def fetch_compose_projects(
    docker: DockerClient, *, project: str | None = None, include_labels: bool = False
) -> ComposeProjects:
    label = f"{COMPOSE_PROJECT_LABEL}={project}" if project else COMPOSE_PROJECT_LABEL
    raw = await docker.get_json(
        "/containers/json", {"all": "true", "filters": json.dumps({"label": [label]})}
    )
    return build_projects(raw, include_labels=include_labels)


def register_compose_tools(mcp: MCPServer, docker: DockerClient) -> None:
    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def get_compose_projects(
        project: Annotated[
            str | None, Field(description="Only the Compose project with this exact name.")
        ] = None,
        include_labels: Annotated[
            bool,
            Field(description="Also list each service's own labels (e.g. Traefik, WUD), masked."),
        ] = False,
    ) -> ComposeProjects:
        """List the Docker Compose projects running on this host with their services.

        Per project: status (running/partial/stopped), the directory and compose files it was
        started from, and per service the image and its containers (stopped ones included).
        Read from container labels, so only projects that have containers are found, and no
        compose file or environment value is ever read. Label values that look sensitive are masked.
        """
        try:
            return await fetch_compose_projects(
                docker, project=project, include_labels=include_labels
            )
        except DockerError as exc:
            raise ToolError(str(exc)) from exc
