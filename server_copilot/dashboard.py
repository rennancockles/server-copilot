"""A read-only status page of the home server, for the LAN and the TV.

It runs as its own process (``server-copilot-dashboard``), never inside the MCP server: the MCP is
reachable from the Internet and this page is not meant to be. Everything it shows comes from the
modules the MCP tools already use, collected in the background every few seconds and served from
memory, so opening the page (or leaving it open on a TV) costs the server nothing extra.
"""

import asyncio
import logging
import os
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from importlib import resources
from typing import Any, Literal, TypeVar

import uvicorn
from pydantic import BaseModel, Field
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from server_copilot.backup import BackupStatus, load_status
from server_copilot.compose_update import UpdateError, UpdateStatus, collect_status
from server_copilot.config import Settings, get_settings
from server_copilot.containers import ContainerSummary, fetch_containers
from server_copilot.diagnostics import ProblemReport, find_problems
from server_copilot.docker import DockerClient, DockerError
from server_copilot.docker_usage import DockerDiskUsage, fetch_docker_disk_usage
from server_copilot.host import HostMetrics, collect_host_metrics, parse_disks
from server_copilot.network import NetworkDevices, list_devices, unknown_devices
from server_copilot.network_scan import RISKY_PORTS
from server_copilot.network_snapshot import read_snapshot
from server_copilot.stats import StatsOverview, fetch_stats_overview

# Under uvicorn.error so these lines reach the container log: nothing else configures logging.
logger = logging.getLogger("uvicorn.error.dashboard")

State = Literal["ok", "warning", "error", "unavailable"]

TOP_CONSUMERS = 5
DISK_WARNING = 85.0
DISK_ERROR = 95.0
MEMORY_WARNING = 90.0
MEMORY_ERROR = 97.0
HOST_TIMEOUT = 10
FILE_TIMEOUT = 10
DOCKER_TIMEOUT = 30
DOCKER_USAGE_TIMEOUT = 60
OFFLINE_SHOWN = 5
UPDATES_SHOWN = 3
RECENT_UPDATE_HOURS = 24.0
SECONDS_PER_HOUR = 3600
STALE_AFTER_REFRESHES = 3
SEVERE_PROBLEMS = {"unhealthy", "restart_loop", "oom_killed", "dead"}
STATE_RANK: dict[State, int] = {"unavailable": 0, "ok": 1, "warning": 2, "error": 3}
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/containers": ("containers.html", "text/html; charset=utf-8"),
    "/network": ("network.html", "text/html; charset=utf-8"),
    "/detail.css": ("detail.css", "text/css; charset=utf-8"),
    "/detail.js": ("detail.js", "text/javascript; charset=utf-8"),
}
# The page loads nothing from anywhere but itself and runs no inline script, so a name taken from
# a container or a device can never become markup.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}


class _Section(BaseModel):
    state: State = "unavailable"
    note: str | None = None


class DiskRow(BaseModel):
    name: str
    percent: float
    used_bytes: int
    total_bytes: int


class HostSection(_Section):
    cpu_percent: float | None = None
    cores: int | None = None
    load_average: list[float] = Field(default_factory=list)
    memory_percent: float | None = None
    memory_used_bytes: int | None = None
    memory_total_bytes: int | None = None
    swap_percent: float | None = None
    uptime_seconds: int | None = None
    disks: list[DiskRow] = Field(default_factory=list)


class ProblemRow(BaseModel):
    name: str
    problems: list[str]


class ConsumerRow(BaseModel):
    name: str
    cpu_percent: float
    memory_usage_bytes: int
    memory_percent: float


class StackContainer(BaseModel):
    name: str
    service: str | None
    image: str
    state: str
    status: str
    cpu_percent: float | None
    memory_usage_bytes: int | None
    problems: list[str]


class StackRow(BaseModel):
    name: str | None
    state: State
    running: int
    containers: list[StackContainer]


class ContainersSection(_Section):
    total: int = 0
    running: int = 0
    stopped: int = 0
    problems: list[ProblemRow] = Field(default_factory=list)
    top_cpu: list[ConsumerRow] = Field(default_factory=list)
    top_memory: list[ConsumerRow] = Field(default_factory=list)
    stacks: list[StackRow] = Field(default_factory=list)


class RepositoryRow(BaseModel):
    name: str
    snapshot_age_hours: float | None
    size_bytes: int | None
    free_bytes: int | None


class BackupSection(_Section):
    status: str | None = None
    summary: str | None = None
    last_success_age_hours: float | None = None
    last_duration_seconds: int | None = None
    integrity: str | None = None
    restore_test: str | None = None
    restore_next_run_at: str | None = None
    repositories: list[RepositoryRow] = Field(default_factory=list)


class TypeRow(BaseModel):
    device_type: str
    count: int


class FindingRow(BaseModel):
    kind: str
    severity: str
    ip: str | None
    detail: str


class OfflineRow(BaseModel):
    name: str
    device_type: str
    age_hours: float | None


class RiskyDeviceRow(BaseModel):
    name: str
    ip: str
    ports: list[int]


class DeviceDetail(BaseModel):
    name: str | None
    ip: str
    mac: str | None
    vendor: str | None
    device_type: str
    open_ports: list[int]
    risky_ports: list[int]
    is_gateway: bool
    is_new: bool
    first_seen: str


class OfflineDetail(BaseModel):
    name: str | None
    ip: str | None
    mac: str | None
    vendor: str | None
    device_type: str
    first_seen: str
    last_seen_age_hours: float | None


class NetworkSection(_Section):
    online: int = 0
    new_devices: int = 0
    stale: bool = False
    by_type: list[TypeRow] = Field(default_factory=list)
    findings: list[FindingRow] = Field(default_factory=list)
    risky_devices: list[RiskyDeviceRow] = Field(default_factory=list)
    offline: list[OfflineRow] = Field(default_factory=list)
    age_minutes: float | None = None
    # Only for the /network page: the TV payload leaves these (and every MAC address) out.
    devices: list[DeviceDetail] = Field(default_factory=list)
    offline_devices: list[OfflineDetail] = Field(default_factory=list)
    all_findings: list[FindingRow] = Field(default_factory=list)


class UsageRow(BaseModel):
    name: str
    size_bytes: int
    reclaimable_bytes: int


class DockerSection(_Section):
    total_bytes: int | None = None
    reclaimable_bytes: int | None = None
    parts: list[UsageRow] = Field(default_factory=list)


class UpdateRow(BaseModel):
    project: str | None
    status: str
    age_hours: float | None
    failed_step: str | None


class UpdatesSection(_Section):
    recent: list[UpdateRow] = Field(default_factory=list)
    waiting: int = 0


class DashboardStatus(BaseModel):
    generated_at: str
    refresh_seconds: int
    overall: State
    host: HostSection
    docker: DockerSection
    updates: UpdatesSection
    containers: ContainersSection
    backup: BackupSection
    network: NetworkSection


def _worst(states: list[State]) -> State:
    return max(states, key=lambda state: STATE_RANK[state], default="unavailable")


def _level(value: float, warning: float, error: float) -> State:
    if value >= error:
        return "error"
    return "warning" if value >= warning else "ok"


def overall_state(sections: list[State]) -> State:
    """The worst state; sections that could not be read only count when nothing could."""
    readable: list[State] = [s for s in sections if s != "unavailable"]
    return _worst(readable) if readable else "unavailable"


# --- one section each -----------------------------------------------------------------------


def _disk_rows(metrics: HostMetrics, devices: dict[str, int]) -> list[DiskRow]:
    """One row per filesystem: mounts of the same one (by device id) are shown together."""
    rows: list[DiskRow] = []
    seen: dict[int, DiskRow] = {}
    for d in metrics.disks:
        device = devices.get(d.name)
        if device is not None and device in seen:
            seen[device].name += f" / {d.name}"
            continue
        row = DiskRow(
            name=d.name, percent=d.percent, used_bytes=d.used_bytes, total_bytes=d.total_bytes
        )
        rows.append(row)
        if device is not None:
            seen[device] = row
    return rows


def _device_ids(disks: dict[str, str]) -> dict[str, int]:
    ids: dict[str, int] = {}
    for name, path in disks.items():
        with suppress(OSError):
            ids[name] = os.stat(path).st_dev
    return ids


def host_section(metrics: HostMetrics, devices: dict[str, int] | None = None) -> HostSection:
    disks = _disk_rows(metrics, devices or {})
    states: list[State] = [_level(d.percent, DISK_WARNING, DISK_ERROR) for d in disks]
    states.append(_level(metrics.memory.percent, MEMORY_WARNING, MEMORY_ERROR))
    return HostSection(
        state=_worst(states),
        cpu_percent=metrics.cpu.percent,
        cores=metrics.cpu.logical_cores,
        load_average=metrics.cpu.load_average,
        memory_percent=metrics.memory.percent,
        memory_used_bytes=metrics.memory.used_bytes,
        memory_total_bytes=metrics.memory.total_bytes,
        swap_percent=metrics.memory.swap_percent,
        uptime_seconds=metrics.uptime_seconds,
        disks=disks,
        note="; ".join(f"{e.name}: {e.error}" for e in metrics.disk_errors) or None,
    )


def _consumer(name: str, cpu: float, memory: int, memory_percent: float) -> ConsumerRow:
    return ConsumerRow(
        name=name, cpu_percent=cpu, memory_usage_bytes=memory, memory_percent=memory_percent
    )


def _stacks(
    listed: list[ContainerSummary], problems: dict[str, list[str]], overview: StatsOverview
) -> list[StackRow]:
    """The containers grouped by compose project; the ones outside any stack go last."""
    usage = {s.name: s for s in overview.containers}
    groups: dict[str | None, list[StackContainer]] = {}
    for c in sorted(listed, key=lambda c: (c.compose_service or c.name, c.name)):
        stat = usage.get(c.name)
        groups.setdefault(c.compose_project, []).append(
            StackContainer(
                name=c.name,
                service=c.compose_service,
                image=c.image,
                state=c.state,
                status=c.status,
                cpu_percent=stat.cpu_percent if stat else None,
                memory_usage_bytes=stat.memory_usage_bytes if stat else None,
                problems=problems.get(c.name, []),
            )
        )
    rows = []
    for name, containers in groups.items():
        kinds = {k for c in containers for k in c.problems}
        state: State = "error" if kinds & SEVERE_PROBLEMS else "warning" if kinds else "ok"
        running = sum(c.state == "running" for c in containers)
        rows.append(StackRow(name=name, state=state, running=running, containers=containers))
    return sorted(rows, key=lambda r: (r.name is None, r.name or ""))


def containers_section(
    listed: list[ContainerSummary], report: ProblemReport, overview: StatsOverview
) -> ContainersSection:
    running = sum(c.state == "running" for c in listed)
    problems = [
        ProblemRow(name=p.name, problems=list(p.problems)) for p in report.problem_containers
    ]
    severe = any(set(p.problems) & SEVERE_PROBLEMS for p in problems)
    state: State = "error" if severe else "warning" if problems else "ok"
    consumers = [
        _consumer(s.name, s.cpu_percent, s.memory_usage_bytes, s.memory_percent)
        for s in overview.containers
    ]
    return ContainersSection(
        state=state,
        total=len(listed),
        running=running,
        stopped=len(listed) - running,
        problems=problems,
        top_cpu=sorted(consumers, key=lambda c: -c.cpu_percent)[:TOP_CONSUMERS],
        top_memory=sorted(consumers, key=lambda c: -c.memory_usage_bytes)[:TOP_CONSUMERS],
        stacks=_stacks(listed, {p.name: p.problems for p in problems}, overview),
    )


def backup_section(status: BackupStatus) -> BackupSection:
    states: dict[str, State] = {
        "ok": "ok",
        "warning": "warning",
        "error": "error",
        "unknown": "unavailable",
    }
    backup = status.backup
    return BackupSection(
        state=states[status.status],
        status=status.status,
        summary=status.summary,
        last_success_age_hours=backup.last_success_age_hours if backup else None,
        last_duration_seconds=backup.duration_seconds if backup else None,
        integrity=status.integrity.state if status.integrity else None,
        restore_test=status.restore_test.state if status.restore_test else None,
        restore_next_run_at=status.restore_test.next_run_at if status.restore_test else None,
        repositories=[
            RepositoryRow(
                name=r.name,
                snapshot_age_hours=r.snapshot_age_hours,
                size_bytes=r.size_bytes,
                free_bytes=r.free_bytes,
            )
            for r in status.repositories
        ],
    )


def _hours_since(moment: str | None, now: datetime) -> float | None:
    if not moment:
        return None
    try:
        then = datetime.fromisoformat(moment)
    except ValueError:
        return None
    return round(max(0.0, (now - then).total_seconds() / SECONDS_PER_HOUR), 1)


def _offline_rows(result: NetworkDevices, now: datetime) -> list[OfflineRow]:
    """Known devices missing from the last scan, worth a glance on the TV.

    Phones and tablets come and go, and a device of unknown type with neither a name nor a vendor
    (only an IP or a default hostname like ``wlan0``) is scanner noise, not something to miss.
    """
    rows = [
        OfflineRow(
            name=d.name or d.hostname or d.vendor or d.ip or "dispositivo",
            device_type=d.device_type,
            age_hours=_hours_since(d.last_seen, now),
        )
        for d in result.offline_devices
        if d.device_type != "phone_or_tablet" and (d.device_type != "unknown" or d.name or d.vendor)
    ]
    return rows[:OFFLINE_SHOWN]


def _ip_key(ip: str | None) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in (ip or "").split("."))
    except ValueError:
        return (999,)


def _device_details(result: NetworkDevices) -> list[DeviceDetail]:
    return [
        DeviceDetail(
            name=d.name or d.hostname,
            ip=d.ip,
            mac=d.mac,
            vendor=d.vendor,
            device_type=d.device_type,
            open_ports=d.open_ports,
            risky_ports=[p for p in d.open_ports if p in RISKY_PORTS],
            is_gateway=d.is_gateway,
            is_new=d.is_new,
            first_seen=d.first_seen,
        )
        for d in sorted(result.devices, key=lambda d: _ip_key(d.ip))
    ]


def _offline_details(result: NetworkDevices, now: datetime) -> list[OfflineDetail]:
    return [
        OfflineDetail(
            name=d.name or d.hostname,
            ip=d.ip,
            mac=d.mac,
            vendor=d.vendor,
            device_type=d.device_type,
            first_seen=d.first_seen,
            last_seen_age_hours=_hours_since(d.last_seen, now),
        )
        for d in sorted(result.offline_devices, key=lambda d: _ip_key(d.ip))
    ]


def _risky_devices(result: NetworkDevices) -> list[RiskyDeviceRow]:
    return [
        RiskyDeviceRow(
            name=d.name or d.hostname or d.vendor or d.ip,
            ip=d.ip,
            ports=[p for p in d.open_ports if p in RISKY_PORTS],
        )
        for d in result.devices
        if any(p in RISKY_PORTS for p in d.open_ports)
    ]


def network_section(result: NetworkDevices, now: datetime) -> NetworkSection:
    """Counts, findings and names of devices that went away: no MAC addresses.

    Risky open ports are listed but never change the state: they are often not fixable, and a page
    that can never turn green stops being read.
    """
    if result.status == "unknown":
        return NetworkSection(note=result.summary)
    findings = [
        FindingRow(kind=f.kind, severity=f.severity, ip=f.ip, detail=f.detail)
        for f in result.findings
        if f.severity != "info" and f.kind != "risky_ports"
    ]
    state: State = "ok"
    if result.status == "stale" or any(f.severity == "warning" for f in findings):
        state = "warning"
    if any(f.severity == "alert" for f in findings):
        state = "error"
    return NetworkSection(
        state=state,
        online=sum(c.count for c in result.counts),
        new_devices=sum(f.kind == "new_device" for f in result.findings),
        by_type=[TypeRow(device_type=c.device_type, count=c.count) for c in result.counts],
        findings=findings,
        risky_devices=_risky_devices(result),
        offline=_offline_rows(result, now),
        stale=result.status == "stale",
        age_minutes=result.age_minutes,
        devices=_device_details(result),
        offline_devices=_offline_details(result, now),
        all_findings=[
            FindingRow(kind=f.kind, severity=f.severity, ip=f.ip, detail=f.detail)
            for f in result.findings
        ],
    )


def docker_section(report: DockerDiskUsage) -> DockerSection:
    return DockerSection(
        state="ok",
        total_bytes=report.total_bytes,
        reclaimable_bytes=report.reclaimable_bytes,
        parts=[
            UsageRow(
                name=name, size_bytes=part.size_bytes, reclaimable_bytes=part.reclaimable_bytes
            )
            for name, part in (
                ("images", report.images),
                ("volumes", report.volumes),
                ("build_cache", report.build_cache),
            )
        ],
    )


def updates_section(status: UpdateStatus, now: datetime) -> UpdatesSection:
    """The latest updates asked of the host; a failure in the last day is a warning."""
    rows = [
        UpdateRow(
            project=u.project,
            status=u.status,
            age_hours=_hours_since(u.finished_at or u.started_at or u.requested_at, now),
            failed_step=u.failed_step,
        )
        for u in status.updates
    ]
    recent_failure = any(
        r.status in ("failed", "rejected")
        and r.age_hours is not None
        and r.age_hours <= RECENT_UPDATE_HOURS
        for r in rows
    )
    return UpdatesSection(
        state="warning" if recent_failure else "ok",
        recent=rows[:UPDATES_SHOWN],
        waiting=sum(r.status in ("queued", "running") for r in rows),
    )


# --- collecting -----------------------------------------------------------------------------

T = TypeVar("T", bound=_Section)


class Dashboard:
    def __init__(
        self,
        settings: Settings,
        docker: DockerClient,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.settings = settings
        self.docker = docker
        self.now = now
        self.disks = parse_disks(settings.host_disks)
        self.status: DashboardStatus | None = None
        self.refreshed_at: datetime | None = None

    async def _guard(self, label: str, empty: type[T], seconds: int, build: Awaitable[T]) -> T:
        """A section that fails or hangs is reported as unavailable; the others are unaffected."""
        try:
            return await asyncio.wait_for(build, seconds)
        except DockerError as exc:
            return empty(note=str(exc))
        except TimeoutError:
            return empty(note="Demorou demais para ler.")
        except Exception as exc:  # noqa: BLE001
            logger.warning("dashboard section %s failed: %s", label, type(exc).__name__)
            return empty(note="Não foi possível ler.")

    async def _host(self) -> HostSection:
        metrics, devices = await asyncio.gather(
            asyncio.to_thread(collect_host_metrics, self.disks),
            asyncio.to_thread(_device_ids, self.disks),
        )
        return host_section(metrics, devices)

    async def _containers(self) -> ContainersSection:
        listed, report, overview = await asyncio.gather(
            fetch_containers(self.docker),
            find_problems(self.docker),
            fetch_stats_overview(self.docker),
        )
        return containers_section(listed, report, overview)

    async def _backup(self) -> BackupSection:
        path = self.settings.backup_status_file
        return backup_section(await asyncio.to_thread(load_status, path, self.now()))

    async def _network(self) -> NetworkSection:
        loaded = await asyncio.to_thread(read_snapshot, self.settings.network_scan_file)
        now = self.now()
        if isinstance(loaded, str):
            return network_section(unknown_devices(loaded), now)
        result = list_devices(loaded, now, device_type=None, query=None, include_offline=True)
        return network_section(result, now)

    async def _docker_usage(self) -> DockerSection:
        return docker_section(await fetch_docker_disk_usage(self.docker, top=1))

    async def _updates(self) -> UpdatesSection:
        requests, results = self.settings.update_requests_dir, self.settings.update_results_dir
        if not requests or not results:
            return UpdatesSection(note="Os updates não estão configurados.")
        now = self.now()
        try:
            status = await asyncio.to_thread(
                collect_status, requests, results, None, now.timestamp()
            )
        except UpdateError as exc:
            return UpdatesSection(note=str(exc))
        return updates_section(status, now)

    async def collect(self) -> DashboardStatus:
        host, docker, updates, containers, backup, network = await asyncio.gather(
            self._guard("host", HostSection, HOST_TIMEOUT, self._host()),
            self._guard("docker", DockerSection, DOCKER_USAGE_TIMEOUT, self._docker_usage()),
            self._guard("updates", UpdatesSection, FILE_TIMEOUT, self._updates()),
            self._guard("containers", ContainersSection, DOCKER_TIMEOUT, self._containers()),
            self._guard("backup", BackupSection, FILE_TIMEOUT, self._backup()),
            self._guard("network", NetworkSection, FILE_TIMEOUT, self._network()),
        )
        states = [s.state for s in (host, docker, updates, containers, backup, network)]
        return DashboardStatus(
            generated_at=self.now().isoformat(timespec="seconds"),
            refresh_seconds=self.settings.dashboard_refresh_seconds,
            overall=overall_state(states),
            host=host,
            docker=docker,
            updates=updates,
            containers=containers,
            backup=backup,
            network=network,
        )

    async def refresh(self) -> None:
        self.status = await self.collect()
        self.refreshed_at = self.now()

    async def run(self) -> None:
        while True:
            try:
                await self.refresh()
            except Exception as exc:  # noqa: BLE001
                logger.error("dashboard refresh failed: %s", type(exc).__name__)
            await asyncio.sleep(self.settings.dashboard_refresh_seconds)

    def age_seconds(self) -> int | None:
        if self.refreshed_at is None:
            return None
        return max(0, int((self.now() - self.refreshed_at).total_seconds()))


# --- the web app ----------------------------------------------------------------------------


def _asset(name: str) -> str:
    return (
        resources.files("server_copilot").joinpath("assets", "dashboard", name).read_text("utf-8")
    )


def create_dashboard_app(dashboard: Dashboard, *, refresh_in_background: bool = True) -> Starlette:
    pages = {path: (_asset(name), media) for path, (name, media) in ASSETS.items()}

    def page(path: str) -> Callable[[Request], Awaitable[Response]]:
        body, media = pages[path]

        async def handler(_: Request) -> Response:
            return Response(body, media_type=media, headers=SECURITY_HEADERS)

        return handler

    def answer(body_of: Callable[[DashboardStatus], dict[str, Any]]) -> Callable[..., Any]:
        async def handler(_: Request) -> Response:
            if dashboard.status is None:
                return JSONResponse(
                    {"overall": "unavailable", "starting": True},
                    status_code=503,
                    headers=SECURITY_HEADERS,
                )
            body = body_of(dashboard.status)
            body["age_seconds"] = dashboard.age_seconds()
            body["stale"] = (dashboard.age_seconds() or 0) > (
                dashboard.settings.dashboard_refresh_seconds * STALE_AFTER_REFRESHES
            )
            return JSONResponse(body, headers=SECURITY_HEADERS)

        return handler

    # The TV payload stays small and carries only what the TV shows; the detail pages ask for more.
    api_status = answer(
        lambda status: status.model_dump(
            exclude={
                "containers": {"stacks"},
                "network": {"devices", "offline_devices", "all_findings"},
            }
        )
    )
    api_network = answer(
        lambda status: {
            "overall": status.overall,
            **status.network.model_dump(
                include={
                    "state",
                    "note",
                    "online",
                    "stale",
                    "age_minutes",
                    "devices",
                    "offline_devices",
                    "all_findings",
                }
            ),
        }
    )
    api_containers = answer(
        lambda status: {
            "overall": status.overall,
            "state": status.containers.state,
            "note": status.containers.note,
            "stacks": [row.model_dump() for row in status.containers.stacks],
        }
    )

    async def health(_: Request) -> Response:
        return JSONResponse({"status": "ok"}, headers=SECURITY_HEADERS)

    async def no_icon(_: Request) -> Response:
        return Response(status_code=204, headers=SECURITY_HEADERS)

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncGenerator[None]:
        task = asyncio.create_task(dashboard.run()) if refresh_in_background else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await dashboard.docker.aclose()

    routes = [Route(path, page(path)) for path in ASSETS]
    routes += [
        Route("/api/status", api_status),
        Route("/api/containers", api_containers),
        Route("/api/network", api_network),
        Route("/health", health),
        Route("/favicon.ico", no_icon),
    ]
    return Starlette(routes=routes, lifespan=lifespan)


def main() -> None:
    settings = get_settings()
    docker = DockerClient(settings.docker_host)
    app = create_dashboard_app(Dashboard(settings, docker))
    uvicorn.run(app, host=settings.host, port=settings.port, access_log=False)


if __name__ == "__main__":
    main()
