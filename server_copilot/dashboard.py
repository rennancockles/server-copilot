"""A read-only status page of the home server, for the LAN and the TV.

It runs as its own process (``server-copilot-dashboard``), never inside the MCP server: the MCP is
reachable from the Internet and this page is not meant to be. Everything it shows comes from the
modules the MCP tools already use, collected in the background every few seconds and served from
memory, so opening the page (or leaving it open on a TV) costs the server nothing extra.
"""

import asyncio
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from importlib import resources
from typing import Literal, TypeVar

import uvicorn
from pydantic import BaseModel, Field
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from server_copilot.backup import BackupStatus, load_status
from server_copilot.config import Settings, get_settings
from server_copilot.containers import ContainerSummary, fetch_containers
from server_copilot.diagnostics import ProblemReport, find_problems
from server_copilot.docker import DockerClient, DockerError
from server_copilot.host import HostMetrics, collect_host_metrics, parse_disks
from server_copilot.network import NetworkDevices, list_devices, unknown_devices
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
STALE_AFTER_REFRESHES = 3
SEVERE_PROBLEMS = {"unhealthy", "restart_loop", "oom_killed", "dead"}
STATE_RANK: dict[State, int] = {"unavailable": 0, "ok": 1, "warning": 2, "error": 3}
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
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


class ContainersSection(_Section):
    total: int = 0
    running: int = 0
    stopped: int = 0
    problems: list[ProblemRow] = Field(default_factory=list)
    top_cpu: list[ConsumerRow] = Field(default_factory=list)
    top_memory: list[ConsumerRow] = Field(default_factory=list)


class RepositoryRow(BaseModel):
    name: str
    snapshot_age_hours: float | None
    free_bytes: int | None


class BackupSection(_Section):
    status: str | None = None
    summary: str | None = None
    last_success_age_hours: float | None = None
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


class NetworkSection(_Section):
    online: int = 0
    new_devices: int = 0
    by_type: list[TypeRow] = Field(default_factory=list)
    findings: list[FindingRow] = Field(default_factory=list)
    age_minutes: float | None = None


class DashboardStatus(BaseModel):
    generated_at: str
    refresh_seconds: int
    overall: State
    host: HostSection
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


def host_section(metrics: HostMetrics) -> HostSection:
    disks = [
        DiskRow(name=d.name, percent=d.percent, used_bytes=d.used_bytes, total_bytes=d.total_bytes)
        for d in metrics.disks
    ]
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
        integrity=status.integrity.state if status.integrity else None,
        restore_test=status.restore_test.state if status.restore_test else None,
        restore_next_run_at=status.restore_test.next_run_at if status.restore_test else None,
        repositories=[
            RepositoryRow(
                name=r.name, snapshot_age_hours=r.snapshot_age_hours, free_bytes=r.free_bytes
            )
            for r in status.repositories
        ],
    )


def network_section(result: NetworkDevices) -> NetworkSection:
    """Counts and findings only: no MAC addresses and no list of devices."""
    if result.status == "unknown":
        return NetworkSection(note=result.summary)
    findings = [
        FindingRow(kind=f.kind, severity=f.severity, ip=f.ip, detail=f.detail)
        for f in result.findings
        if f.severity != "info"
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
        age_minutes=result.age_minutes,
        note="The last scan is old; the scanner may have stopped."
        if result.status == "stale"
        else None,
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
            return empty(note="It took too long to read.")
        except Exception as exc:  # noqa: BLE001
            logger.warning("dashboard section %s failed: %s", label, type(exc).__name__)
            return empty(note="It could not be read.")

    async def _host(self) -> HostSection:
        return host_section(await asyncio.to_thread(collect_host_metrics, self.disks))

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
        if isinstance(loaded, str):
            return network_section(unknown_devices(loaded))
        result = list_devices(
            loaded, self.now(), device_type=None, query=None, include_offline=False
        )
        return network_section(result)

    async def collect(self) -> DashboardStatus:
        host, containers, backup, network = await asyncio.gather(
            self._guard("host", HostSection, HOST_TIMEOUT, self._host()),
            self._guard("containers", ContainersSection, DOCKER_TIMEOUT, self._containers()),
            self._guard("backup", BackupSection, FILE_TIMEOUT, self._backup()),
            self._guard("network", NetworkSection, FILE_TIMEOUT, self._network()),
        )
        return DashboardStatus(
            generated_at=self.now().isoformat(timespec="seconds"),
            refresh_seconds=self.settings.dashboard_refresh_seconds,
            overall=overall_state([host.state, containers.state, backup.state, network.state]),
            host=host,
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

    async def api_status(_: Request) -> Response:
        if dashboard.status is None:
            return JSONResponse(
                {"overall": "unavailable", "starting": True},
                status_code=503,
                headers=SECURITY_HEADERS,
            )
        body = dashboard.status.model_dump()
        body["age_seconds"] = dashboard.age_seconds()
        body["stale"] = (dashboard.age_seconds() or 0) > (
            dashboard.settings.dashboard_refresh_seconds * STALE_AFTER_REFRESHES
        )
        return JSONResponse(body, headers=SECURITY_HEADERS)

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
