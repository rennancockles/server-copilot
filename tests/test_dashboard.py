import asyncio
import json
import re
import time
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
from starlette.testclient import TestClient

from server_copilot import dashboard
from server_copilot.backup import RawStatusFile, evaluate, unknown_status
from server_copilot.compose_update import UpdateResult, UpdateStatus
from server_copilot.config import Settings
from server_copilot.containers import ContainerSummary
from server_copilot.dashboard import (
    Dashboard,
    State,
    backup_section,
    containers_section,
    create_dashboard_app,
    docker_section,
    host_section,
    network_section,
    overall_state,
    updates_section,
)
from server_copilot.diagnostics import ProblemContainer, ProblemKind, ProblemReport
from server_copilot.docker import DockerClient, DockerError
from server_copilot.docker_usage import DockerDiskUsage, UsageSummary
from server_copilot.host import (
    CpuMetrics,
    DiskError,
    DiskUsage,
    HostMetrics,
    MemoryMetrics,
)
from server_copilot.network import list_devices, unknown_devices
from server_copilot.network_snapshot import (
    Classification,
    Device,
    DeviceType,
    Finding,
    OfflineDevice,
    Port,
    ScanInfo,
    ScannedNetwork,
    Snapshot,
)
from server_copilot.stats import ContainerStats, StatsOverview

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
MAC = "f4:7b:5e:00:00:09"


def host_metrics(*, disks: list[float] | None = None, memory: float = 40.0) -> HostMetrics:
    percents = [30.0] if disks is None else disks
    return HostMetrics(
        kernel="6.8.0",
        booted_at="2026-10-01T00:00:00+00:00",
        uptime_seconds=2_000_000,
        cpu=CpuMetrics(
            percent=12.5, logical_cores=8, physical_cores=4, load_average=[0.5, 0.4, 0.3]
        ),
        memory=MemoryMetrics(
            total_bytes=16 * 2**30,
            available_bytes=8 * 2**30,
            used_bytes=8 * 2**30,
            percent=memory,
            swap_total_bytes=2**30,
            swap_used_bytes=0,
            swap_percent=0.0,
        ),
        disks=[
            DiskUsage(
                name=f"d{n}",
                path=f"/host/d{n}",
                total_bytes=1000,
                used_bytes=int(p * 10),
                free_bytes=1000 - int(p * 10),
                percent=p,
            )
            for n, p in enumerate(percents)
        ],
        disk_errors=[],
        notes=[],
    )


def summary(name: str, state: str = "running") -> ContainerSummary:
    return ContainerSummary(
        id=name[:12],
        name=name,
        image="img:1",
        state=state,
        status="Up",
        created=None,
        ports=[],
        compose_project="media",
        compose_service=name,
    )


def problem(name: str, *kinds: str) -> ProblemContainer:
    return ProblemContainer(
        id=name,
        name=name,
        image="img:1",
        problems=[cast(ProblemKind, kind) for kind in kinds],
        state="running",
        health=None,
        restart_count=0,
        exit_code=None,
        oom_killed=False,
        finished_at=None,
        last_health_output=None,
    )


def stats(name: str, cpu: float, memory: int) -> ContainerStats:
    return ContainerStats(
        id=name,
        name=name,
        cpu_percent=cpu,
        memory_usage_bytes=memory,
        memory_limit_bytes=2**33,
        memory_percent=round(memory / 2**33 * 100, 2),
        network_rx_bytes=0,
        network_tx_bytes=0,
        block_read_bytes=0,
        block_write_bytes=0,
        pids=1,
    )


def overview(*items: ContainerStats) -> StatsOverview:
    return StatsOverview(
        containers=list(items),
        total_cpu_percent=sum(i.cpu_percent for i in items),
        total_memory_usage_bytes=sum(i.memory_usage_bytes for i in items),
        errors=[],
    )


def backup_doc(**changes: Any) -> dict[str, Any]:
    def ago(**delta: float) -> str:
        return (NOW - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")

    document: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": ago(hours=1),
        "max_backup_age_hours": 26,
        "backup": {
            "last_run": {
                "started_at": ago(hours=7, minutes=1),
                "finished_at": ago(hours=7),
                "duration_seconds": 48,
                "result": "ok",
                "failed_steps": [],
            },
            "last_success_at": ago(hours=7),
            "repositories": {
                "local": {"newest_snapshot_at": ago(hours=7), "size_bytes": 8, "free_bytes": None},
                "drive": {
                    "newest_snapshot_at": ago(hours=7),
                    "size_bytes": 8,
                    "free_bytes": 30_000_000_000,
                },
            },
        },
        "integrity": {"finished_at": ago(days=2), "duration_seconds": 40, "result": "ok"},
        "restore_test": {
            "finished_at": ago(days=10),
            "duration_seconds": 38,
            "result": "ok",
            "next_run_at": (NOW + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    }
    document.update(changes)
    return document


def snapshot(*findings: Finding, generated: datetime = NOW) -> Snapshot:
    def device(ip: str, mac: str, kind: str) -> Device:
        return Device(
            ip=ip,
            mac=mac,
            vendor="Samsung Electronics",
            name=f"name of {ip}",
            ports=[Port(port=8001)],
            classification=Classification(device_type=cast(DeviceType, kind), confidence="high"),
            first_seen=NOW - timedelta(days=30),
            last_seen=generated,
        )

    return Snapshot(
        schema_version=1,
        generated_at=generated,
        learning_until=NOW - timedelta(days=29),
        scan=ScanInfo(
            started_at=generated - timedelta(seconds=30),
            finished_at=generated,
            duration_seconds=30,
            complete=True,
            interval_seconds=300,
            networks=[
                ScannedNetwork(
                    cidr="192.168.1.0/24", interface="eth0", source="auto", hosts_probed=253
                )
            ],
        ),
        devices=[
            device("192.168.1.9", MAC, "tv"),
            device("192.168.1.50", "00:1b:a9:00:00:50", "printer"),
        ],
        findings=list(findings),
    )


# --- the sections ---------------------------------------------------------------------------


def test_the_host_section_reports_numbers_and_stays_ok_when_nothing_is_full() -> None:
    section = host_section(host_metrics())

    assert section.state == "ok"
    assert (section.cpu_percent, section.cores, section.load_average) == (12.5, 8, [0.5, 0.4, 0.3])
    assert (section.memory_percent, section.memory_total_bytes) == (40.0, 16 * 2**30)
    assert [(d.name, d.percent) for d in section.disks] == [("d0", 30.0)]
    assert section.uptime_seconds == 2_000_000
    assert section.note is None


@pytest.mark.parametrize(
    ("disks", "memory", "state"),
    [
        ([84.9], 89.9, "ok"),
        ([85.0], 10.0, "warning"),
        ([10.0, 95.0], 10.0, "error"),
        ([10.0], 90.0, "warning"),
        ([10.0], 97.0, "error"),
        ([86.0, 99.0], 98.0, "error"),
        ([], 20.0, "ok"),
    ],
)
def test_a_full_disk_or_memory_changes_the_host_state(
    disks: list[float], memory: float, state: str
) -> None:
    assert host_section(host_metrics(disks=disks, memory=memory)).state == state


def test_a_disk_that_cannot_be_read_is_noted() -> None:
    metrics = host_metrics()
    metrics.disk_errors.append(DiskError(name="data", path="/host/data", error="No such file"))

    assert host_section(metrics).note == "data: No such file"


def test_containers_count_running_and_stopped_and_list_the_busiest() -> None:
    listed = [summary("a"), summary("b"), summary("c", "exited")]
    busy = overview(*(stats(f"c{n}", cpu=float(n), memory=(10 - n) * 2**20) for n in range(8)))

    section = containers_section(
        listed, ProblemReport(problem_containers=[], checked=3, errors=[]), busy
    )

    assert (section.state, section.total, section.running, section.stopped) == ("ok", 3, 2, 1)
    assert [c.name for c in section.top_cpu] == ["c7", "c6", "c5", "c4", "c3"]
    assert [c.name for c in section.top_memory] == ["c0", "c1", "c2", "c3", "c4"]
    assert section.problems == []


@pytest.mark.parametrize(
    ("kinds", "state"),
    [
        (["exited_with_error"], "warning"),
        (["unhealthy"], "error"),
        (["restart_loop"], "error"),
        (["oom_killed"], "error"),
        (["dead"], "error"),
        (["exited_with_error", "unhealthy"], "error"),
    ],
)
def test_container_problems_set_the_state(kinds: list[str], state: str) -> None:
    report = ProblemReport(problem_containers=[problem("plex", *kinds)], checked=1, errors=[])

    section = containers_section([summary("plex")], report, overview())

    assert section.state == state
    assert [(p.name, p.problems) for p in section.problems] == [("plex", kinds)]


def test_a_healthy_backup_is_ok_with_ages_and_free_space() -> None:
    status = evaluate(RawStatusFile.model_validate(backup_doc()), NOW)

    section = backup_section(status)

    assert section.state == "ok"
    assert section.status == "ok"
    assert section.last_success_age_hours == 7.0
    assert (section.integrity, section.restore_test) == ("ok", "ok")
    assert section.restore_next_run_at == "2026-11-02T12:00:00+00:00"
    assert {(r.name, r.snapshot_age_hours, r.free_bytes) for r in section.repositories} == {
        ("drive", 7.0, 30_000_000_000),
        ("local", 7.0, None),
    }


def test_a_failed_backup_is_an_error_and_unknown_data_is_unavailable() -> None:
    document = backup_doc()
    document["backup"]["last_run"]["result"] = "failed"
    failed = backup_section(evaluate(RawStatusFile.model_validate(document), NOW))
    missing = backup_section(unknown_status("The backup status file does not exist yet.", NOW))

    assert failed.state == "error"
    assert (missing.state, missing.status) == ("unavailable", "unknown")
    assert missing.summary == "The backup status file does not exist yet."


def network(snap: Snapshot, now: datetime = NOW) -> Any:
    return network_section(
        list_devices(snap, now, device_type=None, query=None, include_offline=True), now
    )


def test_the_network_section_counts_devices_without_listing_them() -> None:
    section = network(snapshot())

    assert (section.state, section.online, section.findings) == ("ok", 2, [])
    assert {(t.device_type, t.count) for t in section.by_type} == {("tv", 1), ("printer", 1)}
    dumped = section.model_dump_json()
    assert MAC not in dumped
    assert "name of" not in dumped


def test_network_findings_set_the_state_and_info_ones_are_not_listed() -> None:
    info = Finding(kind="new_device", severity="info", ip="192.168.1.60", detail="A new camera.")
    warning = Finding(
        kind="risky_ports", severity="warning", ip="192.168.1.77", mac=MAC, detail="Telnet is open."
    )
    alert = Finding(
        kind="gateway_mac_changed", severity="alert", ip="192.168.1.1", detail="Changed."
    )

    assert network(snapshot(info)).state == "ok"
    assert network(snapshot(info)).new_devices == 1
    section = network(snapshot(info, warning))
    assert (section.state, [f.kind for f in section.findings]) == ("warning", ["risky_ports"])
    assert network(snapshot(info, warning, alert)).state == "error"
    assert MAC not in network(snapshot(warning)).model_dump_json()


def test_an_old_scan_is_a_warning_and_a_missing_one_is_unavailable() -> None:
    old = snapshot(generated=NOW - timedelta(hours=2))

    assert network(old).state == "warning"
    assert network(old).stale is True
    assert network(snapshot()).stale is False
    missing = network_section(unknown_devices("No network scan exists yet."), NOW)
    assert (missing.state, missing.note, missing.online) == (
        "unavailable",
        "No network scan exists yet.",
        0,
    )


def offline_device(name: str | None, kind: str, days: float, ip: str = "192.168.1.60") -> Any:
    return OfflineDevice(
        ip=ip,
        mac="aa:bb:cc:00:00:60",
        vendor="Espressif Inc.",
        name=name,
        device_type=cast(DeviceType, kind),
        first_seen=NOW - timedelta(days=60),
        last_seen=NOW - timedelta(days=days),
    )


def test_devices_that_went_away_are_listed_by_name_without_phones_or_macs() -> None:
    snap = snapshot()
    snap.offline_devices = [
        offline_device("Impressora da sala", "printer", 2),
        offline_device("iPhone de visita", "phone_or_tablet", 1),
        offline_device(None, "smart_home", 0.5),
    ]

    section = network(snap)

    assert [(o.name, o.device_type, o.age_hours) for o in section.offline] == [
        ("Impressora da sala", "printer", 48.0),
        ("Espressif Inc.", "smart_home", 12.0),
    ]
    assert "aa:bb:cc" not in section.model_dump_json()


def test_only_a_few_offline_devices_are_kept() -> None:
    snap = snapshot()
    snap.offline_devices = [offline_device(f"device {n}", "camera", n) for n in range(1, 12)]

    assert len(network(snap).offline) == dashboard.OFFLINE_SHOWN


def test_new_devices_are_counted_from_the_findings() -> None:
    new = Finding(kind="new_device", severity="info", ip="192.168.1.60", detail="A new camera.")
    other = Finding(kind="new_device", severity="warning", ip="192.168.1.61", detail="Unknown.")

    assert network(snapshot(new, other)).new_devices == 2


def usage(total: int, reclaimable: int) -> UsageSummary:
    return UsageSummary(count=1, size_bytes=total, reclaimable_bytes=reclaimable)


def disk_usage() -> DockerDiskUsage:
    return DockerDiskUsage(
        images=usage(30 * 2**30, 8 * 2**30),
        containers=usage(2**20, 0),
        volumes=usage(5 * 2**30, 2 * 2**30),
        build_cache=usage(2**30, 2**30),
        total_bytes=36 * 2**30 + 2**20,
        reclaimable_bytes=11 * 2**30,
        cleanup_candidates=[],
        candidates_not_shown=0,
        notes=[],
    )


def test_the_docker_section_reports_use_and_what_could_be_reclaimed() -> None:
    section = docker_section(disk_usage())

    assert section.state == "ok"
    assert (section.total_bytes, section.reclaimable_bytes) == (36 * 2**30 + 2**20, 11 * 2**30)
    assert [(p.name, p.size_bytes, p.reclaimable_bytes) for p in section.parts] == [
        ("images", 30 * 2**30, 8 * 2**30),
        ("volumes", 5 * 2**30, 2 * 2**30),
        ("build_cache", 2**30, 2**30),
    ]


def update_result(
    project: str | None, status: str, hours: float | None, failed_step: str | None = None
) -> UpdateResult:
    moment = None if hours is None else (NOW - timedelta(hours=hours)).isoformat()
    return UpdateResult(
        request_id="a" * 32,
        project=project,
        services=[],
        status=cast(Any, status),
        summary="",
        requested_at=moment,
        started_at=moment,
        finished_at=moment if status in ("succeeded", "failed", "rejected") else None,
        failed_step=failed_step,
        steps=[],
        containers=[],
        output_tail="",
        notes=[],
    )


def updates(*results: UpdateResult) -> UpdateStatus:
    return UpdateStatus(updates=list(results), notes=[])


def test_recent_updates_are_listed_newest_first_and_a_good_one_is_ok() -> None:
    section = updates_section(
        updates(
            update_result("media", "succeeded", 3),
            update_result("kanbai_server", "succeeded", 30),
            update_result("tools", "succeeded", 50),
            update_result("finsight", "succeeded", 90),
        ),
        NOW,
    )

    assert section.state == "ok"
    assert [(r.project, r.status, r.age_hours) for r in section.recent] == [
        ("media", "succeeded", 3.0),
        ("kanbai_server", "succeeded", 30.0),
        ("tools", "succeeded", 50.0),
    ]
    assert section.waiting == 0


def test_a_failed_update_in_the_last_day_is_a_warning_and_an_old_one_is_not() -> None:
    fresh = updates_section(updates(update_result("media", "failed", 5, "pull")), NOW)
    rejected = updates_section(updates(update_result("infra", "rejected", 1)), NOW)
    old = updates_section(updates(update_result("media", "failed", 72, "pull")), NOW)

    assert fresh.state == "warning"
    assert fresh.recent[0].failed_step == "pull"
    assert rejected.state == "warning"
    assert old.state == "ok"
    assert old.recent[0].status == "failed"


def test_updates_waiting_or_running_are_counted_and_a_missing_time_is_tolerated() -> None:
    section = updates_section(
        updates(
            update_result("media", "queued", 0.1),
            update_result("tools", "running", 0.2),
            update_result(None, "succeeded", None),
        ),
        NOW,
    )

    assert (section.state, section.waiting) == ("ok", 2)
    assert section.recent[2].age_hours is None


def test_ages_are_hours_and_unreadable_times_are_tolerated() -> None:
    assert dashboard._hours_since((NOW - timedelta(minutes=90)).isoformat(), NOW) == 1.5
    assert dashboard._hours_since((NOW + timedelta(hours=2)).isoformat(), NOW) == 0.0
    assert dashboard._hours_since("yesterday", NOW) is None
    assert dashboard._hours_since(None, NOW) is None


def test_no_update_yet_is_ok_and_empty() -> None:
    section = updates_section(updates(), NOW)

    assert (section.state, section.recent, section.waiting) == ("ok", [], 0)


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        (["ok", "ok", "ok", "ok"], "ok"),
        (["ok", "warning", "ok", "unavailable"], "warning"),
        (["ok", "warning", "error", "ok"], "error"),
        (["ok", "unavailable", "unavailable", "unavailable"], "ok"),
        (["unavailable"] * 4, "unavailable"),
        ([], "unavailable"),
    ],
)
def test_the_overall_state_is_the_worst_one_that_could_be_read(
    states: list[str], expected: str
) -> None:
    assert overall_state([cast(State, s) for s in states]) == expected


# --- collecting -----------------------------------------------------------------------------


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    backup = tmp_path / "backup-status.json"
    backup.write_text(json.dumps(backup_doc()))
    scan = tmp_path / "scan.json"
    scan.write_text(snapshot().model_dump_json())
    values: dict[str, Any] = {
        "host_disks": "data=/host/data",
        "backup_status_file": str(backup),
        "network_scan_file": str(scan),
    }
    values.update(overrides)
    return Settings(**values)


def make_docker() -> DockerClient:
    return DockerClient(
        "tcp://proxy:2375", transport=httpx2.MockTransport(lambda r: httpx2.Response(200, json=[]))
    )


@pytest.fixture
def sources(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fetch_containers(docker: Any) -> list[ContainerSummary]:
        return [summary("plex"), summary("sonarr")]

    async def find_problems(docker: Any) -> ProblemReport:
        return ProblemReport(problem_containers=[], checked=2, errors=[])

    async def fetch_stats_overview(docker: Any) -> StatsOverview:
        return overview(stats("plex", 20.0, 2**30), stats("sonarr", 2.0, 2**28))

    async def fetch_docker_disk_usage(docker: Any, *, top: int) -> DockerDiskUsage:
        return disk_usage()

    monkeypatch.setattr(dashboard, "fetch_containers", fetch_containers)
    monkeypatch.setattr(dashboard, "find_problems", find_problems)
    monkeypatch.setattr(dashboard, "fetch_stats_overview", fetch_stats_overview)
    monkeypatch.setattr(dashboard, "fetch_docker_disk_usage", fetch_docker_disk_usage)
    monkeypatch.setattr(dashboard, "collect_host_metrics", lambda disks: host_metrics())


def make_dashboard(tmp_path: Path, **overrides: Any) -> Dashboard:
    return Dashboard(make_settings(tmp_path, **overrides), make_docker(), now=lambda: NOW)


async def test_everything_readable_gives_an_ok_status(tmp_path: Path, sources: None) -> None:
    board = make_dashboard(tmp_path)

    status = await board.collect()

    assert status.overall == "ok"
    sections = (status.host, status.docker, status.containers, status.backup, status.network)
    assert [s.state for s in sections] == ["ok"] * 5
    assert status.generated_at == "2026-10-28T12:00:00+00:00"
    assert status.refresh_seconds == 30
    assert (status.containers.running, status.network.online) == (2, 2)
    assert status.containers.top_cpu[0].name == "plex"
    assert status.docker.reclaimable_bytes == 11 * 2**30
    assert (status.backup.last_duration_seconds, status.backup.repositories[0].size_bytes) == (
        48,
        8,
    )


async def test_updates_are_unavailable_until_the_folders_are_configured(
    tmp_path: Path, sources: None
) -> None:
    status = await make_dashboard(tmp_path).collect()

    assert (status.updates.state, status.updates.note) == (
        "unavailable",
        "Os updates não estão configurados.",
    )
    assert status.overall == "ok"


def configured_updates(tmp_path: Path) -> dict[str, Any]:
    requests, results = tmp_path / "requests", tmp_path / "results"
    requests.mkdir()
    results.mkdir()
    return {"update_requests_dir": str(requests), "update_results_dir": str(results)}


def write_update_result(tmp_path: Path, status: str, hours: float) -> None:
    moment = (NOW - timedelta(hours=hours)).isoformat()
    document = {
        "schema_version": 1,
        "id": "a" * 32,
        "project": "media",
        "services": [],
        "requested_at": moment,
        "started_at": moment,
        "finished_at": moment,
        "status": status,
        "failed_step": "pull" if status == "failed" else None,
        "message": "",
        "steps": [],
        "containers": [],
        "output_tail": "",
    }
    (tmp_path / "results" / f"{'a' * 32}.json").write_text(json.dumps(document))


async def test_the_latest_updates_come_from_the_results_folder(
    tmp_path: Path, sources: None
) -> None:
    board = make_dashboard(tmp_path, **configured_updates(tmp_path))
    write_update_result(tmp_path, "succeeded", 4)

    status = await board.collect()

    assert status.updates.state == "ok"
    assert [(r.project, r.status, r.age_hours) for r in status.updates.recent] == [
        ("media", "succeeded", 4.0)
    ]


async def test_a_recent_failed_update_makes_the_whole_board_a_warning(
    tmp_path: Path, sources: None
) -> None:
    board = make_dashboard(tmp_path, **configured_updates(tmp_path))
    write_update_result(tmp_path, "failed", 2)

    status = await board.collect()

    assert status.updates.state == "warning"
    assert status.overall == "warning"


async def test_updates_folders_that_are_not_mounted_are_unavailable_with_the_reason(
    tmp_path: Path, sources: None
) -> None:
    board = make_dashboard(
        tmp_path,
        update_requests_dir=str(tmp_path / "nope-requests"),
        update_results_dir=str(tmp_path / "nope-results"),
    )

    status = await board.collect()

    assert status.updates.state == "unavailable"
    assert "not mounted" in (status.updates.note or "")


async def test_the_docker_usage_failing_only_takes_out_its_own_section(
    tmp_path: Path, sources: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(docker: Any, *, top: int) -> DockerDiskUsage:
        raise DockerError("Enable SYSTEM=1 on the docker-socket-proxy.")

    monkeypatch.setattr(dashboard, "fetch_docker_disk_usage", broken)

    status = await make_dashboard(tmp_path).collect()

    assert (status.docker.state, status.docker.note) == (
        "unavailable",
        "Enable SYSTEM=1 on the docker-socket-proxy.",
    )
    assert (status.host.state, status.containers.state) == ("ok", "ok")
    assert status.overall == "ok"


async def test_a_docker_failure_only_takes_out_the_containers_section(
    tmp_path: Path, sources: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(docker: Any) -> list[ContainerSummary]:
        raise DockerError("Enable CONTAINERS=1 on the docker-socket-proxy.")

    monkeypatch.setattr(dashboard, "fetch_containers", broken)

    status = await make_dashboard(tmp_path).collect()

    assert status.containers.state == "unavailable"
    assert status.containers.note == "Enable CONTAINERS=1 on the docker-socket-proxy."
    assert (status.host.state, status.backup.state, status.network.state) == ("ok", "ok", "ok")
    assert status.overall == "ok"


async def test_a_section_that_hangs_is_cut_off(
    tmp_path: Path, sources: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def slow(docker: Any) -> StatsOverview:
        await asyncio.sleep(30)
        return overview()

    monkeypatch.setattr(dashboard, "fetch_stats_overview", slow)
    monkeypatch.setattr(dashboard, "DOCKER_TIMEOUT", 0.05)
    started = time.monotonic()

    status = await make_dashboard(tmp_path).collect()

    assert time.monotonic() - started < 3
    assert (status.containers.state, status.containers.note) == (
        "unavailable",
        "Demorou demais para ler.",
    )


async def test_an_unexpected_error_is_logged_by_type_and_not_shown(
    tmp_path: Path,
    sources: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def explode(disks: Any) -> HostMetrics:
        raise RuntimeError("secret detail /host/path 192.168.1.9")

    monkeypatch.setattr(dashboard, "collect_host_metrics", explode)

    status = await make_dashboard(tmp_path).collect()

    assert (status.host.state, status.host.note) == ("unavailable", "Não foi possível ler.")
    assert "RuntimeError" in caplog.text
    assert "secret detail" not in caplog.text
    assert "secret detail" not in status.model_dump_json()


async def test_unconfigured_sources_are_unavailable_and_do_not_spoil_the_overall_state(
    tmp_path: Path, sources: None
) -> None:
    board = make_dashboard(tmp_path, backup_status_file="", network_scan_file="")

    status = await board.collect()

    assert (status.backup.state, status.network.state) == ("unavailable", "unavailable")
    assert "SERVER_COPILOT_BACKUP_STATUS_FILE" in (status.backup.summary or "")
    assert "SERVER_COPILOT_NETWORK_SCAN_FILE" in (status.network.note or "")
    assert status.overall == "ok"


async def test_a_failing_backup_makes_the_whole_board_an_error(
    tmp_path: Path, sources: None
) -> None:
    document = backup_doc()
    document["backup"]["last_run"]["result"] = "failed"
    (tmp_path / "backup-status.json").write_text(json.dumps(document))
    board = make_dashboard(tmp_path)
    (tmp_path / "backup-status.json").write_text(json.dumps(document))

    assert (await board.collect()).overall == "error"


async def test_refresh_keeps_the_last_status_and_its_age(tmp_path: Path, sources: None) -> None:
    clock = [NOW]
    board = Dashboard(make_settings(tmp_path), make_docker(), now=lambda: clock[0])
    assert (board.status, board.age_seconds()) == (None, None)

    await board.refresh()
    clock[0] = NOW + timedelta(seconds=42)

    assert board.status is not None
    assert board.age_seconds() == 42


async def test_the_background_loop_refreshes_and_survives_a_failed_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    board = make_dashboard(tmp_path, dashboard_refresh_seconds=10)
    calls = 0
    enough = asyncio.Event()

    async def refresh() -> None:
        nonlocal calls
        calls += 1
        if calls >= 3:
            enough.set()
        if calls == 1:
            raise ValueError("boom 192.168.1.9")

    real_sleep = asyncio.sleep

    async def instant(seconds: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr(board, "refresh", refresh)
    monkeypatch.setattr(dashboard.asyncio, "sleep", instant)
    task = asyncio.create_task(board.run())

    await asyncio.wait_for(enough.wait(), timeout=5)
    task.cancel()

    assert calls >= 3
    assert "dashboard refresh failed: ValueError" in caplog.text
    assert "192.168.1.9" not in caplog.text


def test_the_refresh_interval_is_bounded() -> None:
    assert Settings().dashboard_refresh_seconds == 30
    with pytest.raises(ValueError):
        Settings(dashboard_refresh_seconds=5)
    with pytest.raises(ValueError):
        Settings(dashboard_refresh_seconds=601)


# --- the web app ----------------------------------------------------------------------------


async def ready(tmp_path: Path, **overrides: Any) -> Dashboard:
    board = make_dashboard(tmp_path, **overrides)
    await board.refresh()
    return board


def client(board: Dashboard) -> TestClient:
    return TestClient(create_dashboard_app(board, refresh_in_background=False))


async def test_the_page_and_its_assets_are_served_with_a_strict_policy(
    tmp_path: Path, sources: None
) -> None:
    web = client(await ready(tmp_path))

    for path, media in [
        ("/", "text/html"),
        ("/app.css", "text/css"),
        ("/app.js", "text/javascript"),
    ]:
        response = web.get(path)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith(media)
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-frame-options"] == "DENY"
        policy = response.headers["content-security-policy"]
        assert "default-src 'none'" in policy
        assert "script-src 'self'" in policy
        assert "unsafe-inline" not in policy
        assert "unsafe-eval" not in policy
    assert "Home Server" in web.get("/").text


async def test_the_status_api_has_the_four_sections_and_the_age(
    tmp_path: Path, sources: None
) -> None:
    web = client(await ready(tmp_path))

    response = web.get("/api/status")

    assert response.status_code == 200
    body = response.json()
    assert set(body) >= {
        "overall",
        "host",
        "docker",
        "updates",
        "containers",
        "backup",
        "network",
        "age_seconds",
    }
    assert (body["overall"], body["age_seconds"], body["stale"]) == ("ok", 0, False)
    assert body["containers"]["running"] == 2
    assert response.headers["cache-control"] == "no-store"


async def test_the_api_says_when_it_has_not_collected_anything_yet(tmp_path: Path) -> None:
    web = client(make_dashboard(tmp_path))

    response = web.get("/api/status")

    assert response.status_code == 503
    assert response.json() == {"overall": "unavailable", "starting": True}


async def test_the_api_flags_data_that_stopped_refreshing(tmp_path: Path, sources: None) -> None:
    clock = [NOW]
    board = Dashboard(make_settings(tmp_path), make_docker(), now=lambda: clock[0])
    await board.refresh()
    web = client(board)

    clock[0] = NOW + timedelta(seconds=89)
    assert web.get("/api/status").json()["stale"] is False
    clock[0] = NOW + timedelta(seconds=91)
    body = web.get("/api/status").json()

    assert (body["stale"], body["age_seconds"]) == (True, 91)


async def test_the_api_never_carries_mac_addresses_or_device_names(
    tmp_path: Path, sources: None
) -> None:
    web = client(await ready(tmp_path))

    text = web.get("/api/status").text

    assert MAC not in text
    assert "00:1b:a9" not in text
    assert "name of" not in text


async def test_only_reading_is_allowed_and_unknown_paths_are_not_found(
    tmp_path: Path, sources: None
) -> None:
    web = client(await ready(tmp_path))

    for method in ("post", "put", "delete", "patch"):
        assert getattr(web, method)("/api/status").status_code == 405
        assert getattr(web, method)("/").status_code == 405
    assert web.get("/etc/passwd").status_code == 404
    assert web.get("/../../etc/passwd").status_code == 404
    assert web.get("/app.js/../index.html").status_code in (200, 404)
    assert web.get("/mcp").status_code == 404
    assert web.get("/health").json() == {"status": "ok"}
    assert web.get("/favicon.ico").status_code == 204


def test_the_page_runs_no_inline_code_and_never_builds_markup_from_data() -> None:
    root = resources.files("server_copilot").joinpath("assets", "dashboard")
    html = root.joinpath("index.html").read_text("utf-8")
    script = root.joinpath("app.js").read_text("utf-8")

    assert re.findall(r"<script[^>]*>", html) == ['<script src="/app.js">']
    assert "<style" not in html
    assert re.search(r"\son\w+=", html) is None
    for forbidden in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "eval(",
        "document.write",
        "new Function",
    ):
        assert forbidden not in script
    assert "http://" not in html + script
    assert "https://" not in html + script


async def test_the_background_refresh_runs_with_the_app_and_the_docker_client_is_closed(
    tmp_path: Path, sources: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = make_dashboard(tmp_path)
    closed = []

    async def aclose() -> None:
        closed.append(True)

    monkeypatch.setattr(board.docker, "aclose", aclose)
    app = create_dashboard_app(board)

    with TestClient(app) as web:
        for _ in range(100):
            if web.get("/api/status").status_code == 200:
                break
            await asyncio.sleep(0.05)
        assert web.get("/api/status").status_code == 200

    assert closed == [True]


def test_main_serves_on_the_configured_port(monkeypatch: pytest.MonkeyPatch) -> None:
    served: dict[str, Any] = {}

    def run(app: Any, **kwargs: Any) -> None:
        served.update(kwargs, app=app)

    monkeypatch.setenv("SERVER_COPILOT_PORT", "8081")
    monkeypatch.setenv("SERVER_COPILOT_HOST", "127.0.0.1")
    monkeypatch.setattr(dashboard.uvicorn, "run", run)

    dashboard.main()

    assert (served["host"], served["port"], served["access_log"]) == ("127.0.0.1", 8081, False)
    assert served["app"] is not None
