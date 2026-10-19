import asyncio
import json
from collections.abc import Callable
from typing import Annotated, Any, Literal
from urllib.parse import quote

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from server_copilot.docker import DockerClient, DockerError, DockerNotFoundError

StatsSort = Literal["cpu", "memory", "name"]

SHORT_ID_LENGTH = 12
STATS_CONCURRENCY = 8
PERCENT_DIGITS = 2


class ContainerStats(BaseModel):
    id: str
    name: str
    cpu_percent: float
    memory_usage_bytes: int
    memory_limit_bytes: int
    memory_percent: float
    network_rx_bytes: int
    network_tx_bytes: int
    block_read_bytes: int
    block_write_bytes: int
    pids: int


class StatsError(BaseModel):
    container: str
    error: str


class StatsOverview(BaseModel):
    containers: list[ContainerStats]
    total_cpu_percent: float
    total_memory_usage_bytes: int
    errors: list[StatsError]


def _cpu_percent(raw: dict[str, Any]) -> float:
    cpu = raw.get("cpu_stats") or {}
    previous = raw.get("precpu_stats") or {}
    cpu_delta = (cpu.get("cpu_usage") or {}).get("total_usage", 0) - (
        previous.get("cpu_usage") or {}
    ).get("total_usage", 0)
    previous_system = previous.get("system_cpu_usage") or 0
    system_delta = (cpu.get("system_cpu_usage") or 0) - previous_system
    if previous_system == 0 or cpu_delta <= 0 or system_delta <= 0:
        return 0.0
    online_cpus = cpu.get("online_cpus") or len(
        (cpu.get("cpu_usage") or {}).get("percpu_usage") or []
    )
    return float(round(cpu_delta / system_delta * (online_cpus or 1) * 100, PERCENT_DIGITS))


def _memory(raw: dict[str, Any]) -> tuple[int, int, float]:
    memory = raw.get("memory_stats") or {}
    detail = memory.get("stats") or {}
    # Same as `docker stats`: usage minus reclaimable page cache (cgroup v1 or v2 key).
    cache = detail.get("total_inactive_file", detail.get("inactive_file", 0))
    used = max(memory.get("usage", 0) - cache, 0)
    limit = memory.get("limit", 0)
    percent = round(used / limit * 100, PERCENT_DIGITS) if limit > 0 else 0.0
    return used, limit, percent


def _network(raw: dict[str, Any]) -> tuple[int, int]:
    networks = (raw.get("networks") or {}).values()
    return (
        sum(n.get("rx_bytes", 0) for n in networks),
        sum(n.get("tx_bytes", 0) for n in networks),
    )


def _block_io(raw: dict[str, Any]) -> tuple[int, int]:
    entries = (raw.get("blkio_stats") or {}).get("io_service_bytes_recursive") or []
    totals = {"read": 0, "write": 0}
    for entry in entries:
        operation = str(entry.get("op", "")).lower()
        if operation in totals:
            totals[operation] += entry.get("value", 0)
    return totals["read"], totals["write"]


def compute_stats(raw: dict[str, Any]) -> ContainerStats:
    used, limit, memory_percent = _memory(raw)
    rx, tx = _network(raw)
    block_read, block_write = _block_io(raw)
    return ContainerStats(
        id=raw.get("id", "")[:SHORT_ID_LENGTH],
        name=(raw.get("name") or "").lstrip("/"),
        cpu_percent=_cpu_percent(raw),
        memory_usage_bytes=used,
        memory_limit_bytes=limit,
        memory_percent=memory_percent,
        network_rx_bytes=rx,
        network_tx_bytes=tx,
        block_read_bytes=block_read,
        block_write_bytes=block_write,
        pids=(raw.get("pids_stats") or {}).get("current", 0),
    )


async def fetch_container_stats(docker: DockerClient, container: str) -> ContainerStats:
    container = container.strip()
    if not container:
        raise DockerError("Container name or ID must not be empty.")
    # Not one-shot: Docker then samples twice, which CPU % needs (one-shot zeroes precpu_stats).
    raw = await docker.get_json(
        f"/containers/{quote(container, safe='')}/stats", {"stream": "false"}
    )
    return compute_stats(raw)


def _sort_key(sort_by: StatsSort) -> Callable[[ContainerStats], str | float]:
    if sort_by == "name":
        return lambda stats: stats.name
    if sort_by == "memory":
        return lambda stats: -stats.memory_usage_bytes
    return lambda stats: -stats.cpu_percent


async def fetch_stats_overview(
    docker: DockerClient, *, sort_by: StatsSort = "cpu"
) -> StatsOverview:
    running = await docker.get_json(
        "/containers/json", {"filters": json.dumps({"status": ["running"]})}
    )
    semaphore = asyncio.Semaphore(STATS_CONCURRENCY)

    async def one(item: dict[str, Any]) -> ContainerStats | StatsError:
        name = (item.get("Names") or [item["Id"]])[0].lstrip("/")
        async with semaphore:
            try:
                return await fetch_container_stats(docker, item["Id"])
            except DockerNotFoundError:
                return StatsError(container=name, error="Container disappeared while sampling.")
            except DockerError as exc:
                return StatsError(container=name, error=str(exc))

    results = await asyncio.gather(*(one(item) for item in running))
    stats = sorted((r for r in results if isinstance(r, ContainerStats)), key=_sort_key(sort_by))
    return StatsOverview(
        containers=stats,
        total_cpu_percent=round(sum(s.cpu_percent for s in stats), PERCENT_DIGITS),
        total_memory_usage_bytes=sum(s.memory_usage_bytes for s in stats),
        errors=[r for r in results if isinstance(r, StatsError)],
    )


def register_stats_tools(mcp: MCPServer, docker: DockerClient) -> None:
    read_only = ToolAnnotations(read_only_hint=True)

    @mcp.tool(annotations=read_only)
    async def get_container_stats(
        container: Annotated[str, Field(description="Container name or ID.")],
    ) -> ContainerStats:
        """Get CPU %, memory, network and block I/O usage of one container.

        Takes about 1-2 seconds because Docker samples CPU twice. cpu_percent is relative
        to one core per 100 (a container using 2 full cores shows 200).
        """
        try:
            return await fetch_container_stats(docker, container)
        except DockerError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool(annotations=read_only)
    async def get_containers_overview(
        sort_by: Annotated[
            StatsSort, Field(description="Sort order: highest CPU, highest memory, or name.")
        ] = "cpu",
    ) -> StatsOverview:
        """Get resource usage of every running container at once, with totals.

        Containers that fail to sample are listed in 'errors' instead of failing the call.
        """
        try:
            return await fetch_stats_overview(docker, sort_by=sort_by)
        except DockerError as exc:
            raise ToolError(str(exc)) from exc
