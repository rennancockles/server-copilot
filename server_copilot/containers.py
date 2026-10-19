import json
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from urllib.parse import quote

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from server_copilot.docker import DockerClient, DockerError
from server_copilot.masking import mask_value

ContainerStatus = Literal[
    "created", "restarting", "running", "removing", "paused", "exited", "dead"
]

COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
COMPOSE_SERVICE_LABEL = "com.docker.compose.service"
SHORT_ID_LENGTH = 12
HEALTH_OUTPUT_LIMIT = 500


class ContainerSummary(BaseModel):
    id: str
    name: str
    image: str
    state: str
    status: str
    created: str | None
    ports: list[str]
    compose_project: str | None
    compose_service: str | None


class ContainerState(BaseModel):
    status: str
    running: bool
    paused: bool
    restarting: bool
    oom_killed: bool
    exit_code: int | None
    error: str | None
    started_at: str | None
    finished_at: str | None


class ContainerHealth(BaseModel):
    status: str
    failing_streak: int
    last_output: str | None


class NetworkInfo(BaseModel):
    name: str
    ip_address: str | None
    gateway: str | None
    aliases: list[str]


class MountInfo(BaseModel):
    type: str
    source: str | None
    destination: str
    read_only: bool


class EnvVar(BaseModel):
    name: str
    value: str


class ContainerDetails(BaseModel):
    id: str
    name: str
    image: str
    image_id: str
    created: str | None
    entrypoint: list[str]
    command: list[str]
    state: ContainerState
    health: ContainerHealth | None
    restart_count: int
    restart_policy: str | None
    ports: list[str]
    networks: list[NetworkInfo]
    mounts: list[MountInfo]
    env: list[EnvVar]
    labels: dict[str, str]


def _clean(value: Any) -> str | None:
    return value or None


def _iso_from_epoch(epoch: int | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _host_port(ip: str, port: str | int) -> str:
    return f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}"


def _summary_ports(ports: list[dict[str, Any]]) -> list[str]:
    formatted = []
    for port in ports:
        private = f"{port['PrivatePort']}/{port.get('Type', 'tcp')}"
        if public := port.get("PublicPort"):
            formatted.append(f"{_host_port(port.get('IP', '0.0.0.0'), public)}->{private}")
        else:
            formatted.append(private)
    return _unique(formatted)


def _inspect_ports(ports: dict[str, list[dict[str, str]] | None]) -> list[str]:
    formatted = []
    for container_port, bindings in ports.items():
        if not bindings:
            formatted.append(container_port)
            continue
        for binding in bindings:
            host = _host_port(binding.get("HostIp") or "0.0.0.0", binding["HostPort"])
            formatted.append(f"{host}->{container_port}")
    return _unique(formatted)


def _mask_labels(labels: dict[str, str]) -> dict[str, str]:
    return {key: mask_value(key, value) for key, value in labels.items()}


def _mask_env(entries: list[str]) -> list[EnvVar]:
    env = []
    for entry in entries:
        name, _, value = entry.partition("=")
        env.append(EnvVar(name=name, value=mask_value(name, value)))
    return env


def to_summary(raw: dict[str, Any]) -> ContainerSummary:
    labels: dict[str, str] = raw.get("Labels") or {}
    names: list[str] = raw.get("Names") or []
    return ContainerSummary(
        id=raw["Id"][:SHORT_ID_LENGTH],
        name=names[0].lstrip("/") if names else raw["Id"][:SHORT_ID_LENGTH],
        image=raw.get("Image", ""),
        state=raw.get("State", "unknown"),
        status=raw.get("Status", ""),
        created=_iso_from_epoch(raw.get("Created")),
        ports=_summary_ports(raw.get("Ports") or []),
        compose_project=labels.get(COMPOSE_PROJECT_LABEL),
        compose_service=labels.get(COMPOSE_SERVICE_LABEL),
    )


def _to_health(state: dict[str, Any]) -> ContainerHealth | None:
    health = state.get("Health")
    if not health:
        return None
    log = health.get("Log") or []
    output = log[-1].get("Output", "").strip()[:HEALTH_OUTPUT_LIMIT] if log else None
    return ContainerHealth(
        status=health.get("Status", "unknown"),
        failing_streak=health.get("FailingStreak", 0),
        last_output=output or None,
    )


def to_details(raw: dict[str, Any]) -> ContainerDetails:
    config: dict[str, Any] = raw.get("Config") or {}
    state: dict[str, Any] = raw.get("State") or {}
    network_settings: dict[str, Any] = raw.get("NetworkSettings") or {}
    host_config: dict[str, Any] = raw.get("HostConfig") or {}
    networks: dict[str, dict[str, Any]] = network_settings.get("Networks") or {}
    restart_policy = (host_config.get("RestartPolicy") or {}).get("Name")
    return ContainerDetails(
        id=raw["Id"][:SHORT_ID_LENGTH],
        name=(raw.get("Name") or "").lstrip("/"),
        image=config.get("Image", ""),
        image_id=raw.get("Image", ""),
        created=_clean(raw.get("Created")),
        entrypoint=config.get("Entrypoint") or [],
        command=config.get("Cmd") or [],
        state=ContainerState(
            status=state.get("Status", "unknown"),
            running=state.get("Running", False),
            paused=state.get("Paused", False),
            restarting=state.get("Restarting", False),
            oom_killed=state.get("OOMKilled", False),
            exit_code=state.get("ExitCode"),
            error=_clean(state.get("Error")),
            started_at=_clean(state.get("StartedAt")),
            finished_at=_clean(state.get("FinishedAt")),
        ),
        health=_to_health(state),
        restart_count=raw.get("RestartCount", 0),
        restart_policy=_clean(restart_policy),
        ports=_inspect_ports(network_settings.get("Ports") or {}),
        networks=[
            NetworkInfo(
                name=name,
                ip_address=_clean(network.get("IPAddress")),
                gateway=_clean(network.get("Gateway")),
                aliases=network.get("Aliases") or [],
            )
            for name, network in networks.items()
        ],
        mounts=[
            MountInfo(
                type=mount.get("Type", ""),
                source=_clean(mount.get("Source") or mount.get("Name")),
                destination=mount.get("Destination", ""),
                read_only=not mount.get("RW", True),
            )
            for mount in raw.get("Mounts") or []
        ],
        env=_mask_env(config.get("Env") or []),
        labels=_mask_labels(config.get("Labels") or {}),
    )


async def fetch_containers(
    docker: DockerClient,
    *,
    status: ContainerStatus | None = None,
    name: str | None = None,
    label: str | None = None,
    include_stopped: bool = True,
) -> list[ContainerSummary]:
    filters: dict[str, list[str]] = {}
    if status:
        filters["status"] = [status]
    if name:
        filters["name"] = [name]
    if label:
        filters["label"] = [label]
    params: dict[str, Any] = {"all": "true" if include_stopped or status else "false"}
    if filters:
        params["filters"] = json.dumps(filters)
    raw = await docker.get_json("/containers/json", params)
    return [to_summary(item) for item in raw]


async def fetch_container(docker: DockerClient, container: str) -> ContainerDetails:
    container = container.strip()
    if not container:
        raise DockerError("Container name or ID must not be empty.")
    raw = await docker.get_json(f"/containers/{quote(container, safe='')}/json")
    return to_details(raw)


def register_container_tools(mcp: MCPServer, docker: DockerClient) -> None:
    read_only = ToolAnnotations(read_only_hint=True)

    @mcp.tool(annotations=read_only)
    async def list_containers(
        status: Annotated[
            ContainerStatus | None, Field(description="Only containers in this state.")
        ] = None,
        name: Annotated[
            str | None, Field(description="Only containers whose name matches this text.")
        ] = None,
        label: Annotated[
            str | None,
            Field(
                description="Only containers with this label: 'key' or 'key=value' "
                "(e.g. 'com.docker.compose.project=media')."
            ),
        ] = None,
        include_stopped: Annotated[
            bool, Field(description="Include stopped/exited containers, not just running ones.")
        ] = True,
    ) -> list[ContainerSummary]:
        """List Docker containers with state, image, ports and compose project/service."""
        try:
            return await fetch_containers(
                docker,
                status=status,
                name=name,
                label=label,
                include_stopped=include_stopped,
            )
        except DockerError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool(annotations=read_only)
    async def inspect_container(
        container: Annotated[str, Field(description="Container name or ID.")],
    ) -> ContainerDetails:
        """Inspect a container: state, health, restarts, ports, networks, mounts and env.

        Values of sensitive environment variables and labels (passwords, tokens, keys, ...)
        are masked.
        """
        try:
            return await fetch_container(docker, container)
        except DockerError as exc:
            raise ToolError(str(exc)) from exc
