import asyncio
import platform
from datetime import UTC, datetime

import psutil
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel

CPU_SAMPLE_SECONDS = 0.5
PERCENT_DIGITS = 1


class CpuMetrics(BaseModel):
    percent: float
    logical_cores: int
    physical_cores: int | None
    load_average: list[float]


class MemoryMetrics(BaseModel):
    total_bytes: int
    available_bytes: int
    used_bytes: int
    percent: float
    swap_total_bytes: int
    swap_used_bytes: int
    swap_percent: float


class DiskUsage(BaseModel):
    name: str
    path: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    percent: float


class DiskError(BaseModel):
    name: str
    path: str
    error: str


class HostMetrics(BaseModel):
    kernel: str
    booted_at: str
    uptime_seconds: int
    cpu: CpuMetrics
    memory: MemoryMetrics
    disks: list[DiskUsage]
    disk_errors: list[DiskError]
    notes: list[str]


def parse_disks(spec: str, kind: str = "host disk") -> dict[str, str]:
    """Parse 'name=/path,other=/path2' (a bare '/path' uses the path as its name)."""
    disks: dict[str, str] = {}
    for raw_entry in spec.split(","):
        entry = raw_entry.strip()
        if not entry:
            continue
        name, sep, path = entry.partition("=")
        if not sep:
            name, path = entry, entry
        name, path = name.strip(), path.strip()
        if not name or not path.startswith("/"):
            raise ValueError(f"Invalid {kind} entry {entry!r}; expected 'name=/absolute/path'")
        if name in disks:
            raise ValueError(f"Duplicate {kind} name {name!r}")
        disks[name] = path
    return disks


def _disk_usage(name: str, path: str) -> DiskUsage | DiskError:
    try:
        usage = psutil.disk_usage(path)
    except OSError as exc:
        return DiskError(name=name, path=path, error=f"{exc.strerror or exc}")
    return DiskUsage(
        name=name,
        path=path,
        total_bytes=usage.total,
        used_bytes=usage.used,
        free_bytes=usage.free,
        percent=round(usage.percent, PERCENT_DIGITS),
    )


def collect_host_metrics(disks: dict[str, str]) -> HostMetrics:
    cpu_percent = psutil.cpu_percent(interval=CPU_SAMPLE_SECONDS)
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    boot_time = datetime.fromtimestamp(psutil.boot_time(), tz=UTC)
    disk_results = [_disk_usage(name, path) for name, path in disks.items()]
    notes = []
    if not disks:
        notes.append(
            "No disks configured: set SERVER_COPILOT_HOST_DISKS=name=/container/path,... "
            "and bind-mount each host filesystem read-only."
        )
    return HostMetrics(
        kernel=platform.release(),
        booted_at=boot_time.isoformat(),
        uptime_seconds=int((datetime.now(UTC) - boot_time).total_seconds()),
        cpu=CpuMetrics(
            percent=round(cpu_percent, PERCENT_DIGITS),
            logical_cores=psutil.cpu_count(logical=True) or 0,
            physical_cores=psutil.cpu_count(logical=False),
            load_average=[round(load, 2) for load in psutil.getloadavg()],
        ),
        memory=MemoryMetrics(
            total_bytes=memory.total,
            available_bytes=memory.available,
            used_bytes=memory.total - memory.available,
            percent=round(memory.percent, PERCENT_DIGITS),
            swap_total_bytes=swap.total,
            swap_used_bytes=swap.used,
            swap_percent=round(swap.percent, PERCENT_DIGITS),
        ),
        disks=[r for r in disk_results if isinstance(r, DiskUsage)],
        disk_errors=[r for r in disk_results if isinstance(r, DiskError)],
        notes=notes,
    )


def register_host_tools(mcp: MCPServer, disks: dict[str, str]) -> None:
    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def get_host_metrics() -> HostMetrics:
        """Get host CPU %, load average, memory, swap, uptime and disk usage per configured mount.

        CPU, memory, load and uptime describe the host (the container shares its kernel).
        Disks are the host filesystems bind-mounted read-only and listed in
        SERVER_COPILOT_HOST_DISKS. Takes about half a second to sample CPU.
        """
        return await asyncio.to_thread(collect_host_metrics, disks)
