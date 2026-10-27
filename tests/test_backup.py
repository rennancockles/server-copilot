import copy
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest
from mcp.types import CallToolResult

from server_copilot import backup
from server_copilot.backup import RawStatusFile, evaluate, load_status
from server_copilot.docker import DockerClient
from server_copilot.server import create_server

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


def ago(**delta: float) -> str:
    return (NOW - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def healthy() -> dict[str, Any]:
    return {
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
                "local": {
                    "newest_snapshot_at": ago(hours=7),
                    "size_bytes": 800_000_000,
                    "free_bytes": None,
                },
                "drive": {
                    "newest_snapshot_at": ago(hours=7),
                    "size_bytes": 790_000_000,
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


def status_of(document: dict[str, Any]) -> backup.BackupStatus:
    return evaluate(RawStatusFile.model_validate(document), NOW)


def test_everything_ok() -> None:
    status = status_of(healthy())

    assert status.status == "ok"
    assert status.problems == []
    assert status.summary.startswith("All checks passed")
    assert status.backup is not None
    assert status.backup.state == "ok"
    assert status.backup.last_result == "ok"
    assert status.backup.duration_seconds == 48
    assert status.backup.last_success_age_hours == 7.0
    assert [r.name for r in status.repositories] == ["drive", "local"]
    drive = status.repositories[0]
    assert drive.snapshot_age_hours == 7.0
    assert drive.free_bytes == 30_000_000_000
    assert status.restore_test is not None
    assert status.restore_test.next_run_at == "2026-11-01T12:00:00+00:00"


def test_timestamps_carry_their_timezone() -> None:
    status = status_of(healthy())

    assert status.checked_at == "2026-10-27T12:00:00+00:00"
    assert status.status_file_generated_at == "2026-10-27T11:00:00+00:00"
    assert status.backup is not None
    assert status.backup.finished_at == "2026-10-27T05:00:00+00:00"
    assert status.repositories[0].newest_snapshot_at == "2026-10-27T05:00:00+00:00"


def test_a_failed_last_run_is_an_error_and_names_the_steps() -> None:
    doc = healthy()
    doc["backup"]["last_run"].update(
        result="failed", failed_steps=["restic snapshots", "retention"]
    )

    status = status_of(doc)

    assert status.status == "error"
    assert status.backup is not None
    assert status.backup.state == "error"
    assert status.backup.failed_steps == ["restic snapshots", "retention"]
    assert "restic snapshots, retention" in status.summary
    assert status.backup.last_success_at is not None


def test_an_old_last_success_is_an_error() -> None:
    doc = healthy()
    doc["backup"]["last_success_at"] = ago(hours=30)

    status = status_of(doc)

    assert status.status == "error"
    assert "30 h old (limit 26 h)" in status.summary


def test_no_recorded_success_is_an_error() -> None:
    doc = healthy()
    doc["backup"]["last_success_at"] = None

    status = status_of(doc)

    assert status.status == "error"
    assert any("No successful backup" in p for p in status.problems)


def test_no_recorded_run_is_an_error() -> None:
    doc = healthy()
    doc["backup"]["last_run"] = None

    status = status_of(doc)

    assert status.status == "error"
    assert status.backup is not None
    assert status.backup.last_result is None
    assert any("No backup run" in p for p in status.problems)


def test_an_unreadable_or_old_repository_is_an_error() -> None:
    doc = healthy()
    doc["backup"]["repositories"]["drive"]["newest_snapshot_at"] = None
    doc["backup"]["repositories"]["local"]["newest_snapshot_at"] = ago(hours=40)

    status = status_of(doc)

    assert status.status == "error"
    assert any("drive repository has no readable snapshot" in p for p in status.problems)
    assert any("local snapshot is 40 h old" in p for p in status.problems)


def test_a_status_without_repositories_is_an_error() -> None:
    doc = healthy()
    doc["backup"]["repositories"] = {}

    status = status_of(doc)

    assert status.status == "error"
    assert any("no backup repository" in p for p in status.problems)


def test_a_recent_run_is_not_enough_when_the_restore_test_failed() -> None:
    doc = healthy()
    doc["restore_test"]["result"] = "failed"

    status = status_of(doc)

    assert status.status == "error"
    assert status.backup is not None
    assert status.backup.state == "ok"
    assert status.restore_test is not None
    assert status.restore_test.state == "error"


@pytest.mark.parametrize("key", ["integrity", "restore_test"])
def test_a_check_that_never_ran_is_a_warning_not_ok(key: str) -> None:
    doc = healthy()
    doc[key] = None

    status = status_of(doc)

    assert status.status == "warning"
    assert any("has been recorded yet" in p for p in status.problems)


def test_an_old_restore_test_and_an_old_integrity_check_are_warnings() -> None:
    doc = healthy()
    doc["restore_test"]["finished_at"] = ago(days=40)
    doc["integrity"]["finished_at"] = ago(days=12)

    status = status_of(doc)

    assert status.status == "warning"
    assert status.restore_test is not None
    assert status.restore_test.state == "warning"
    assert status.integrity is not None
    assert status.integrity.state == "warning"
    assert "40 days ago" in status.problems[0] or "40 days ago" in status.problems[1]


def test_a_stale_status_file_is_flagged() -> None:
    doc = healthy()
    doc["generated_at"] = ago(hours=30)

    status = status_of(doc)

    assert status.status == "warning"
    assert status.status_file_age_hours == 30.0
    assert any("status file was last refreshed" in p for p in status.problems)


def test_the_age_limit_comes_from_the_file() -> None:
    doc = healthy()
    doc["max_backup_age_hours"] = 6

    status = status_of(doc)

    assert status.status == "error"
    assert status.backup is not None
    assert status.backup.max_age_hours == 6


def test_a_timestamp_in_the_future_gives_age_zero() -> None:
    doc = healthy()
    doc["backup"]["last_success_at"] = (NOW + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

    status = status_of(doc)

    assert status.backup is not None
    assert status.backup.last_success_age_hours == 0.0


def test_unknown_keys_and_odd_steps_never_reach_the_response() -> None:
    doc = healthy()
    doc["rclone_token"] = "SECRET-TOKEN-123"
    doc["backup"]["restic_password"] = "SECRET-PASSWORD-456"
    doc["backup"]["last_run"].update(
        result="failed",
        failed_steps=["restic snapshots; echo $(cat /etc/shadow) > /tmp/x", "a" * 500] + ["x"] * 20,
    )

    status = status_of(doc)
    dumped = status.model_dump_json()

    assert "SECRET" not in dumped
    assert "/etc/shadow" not in dumped
    assert "$" not in dumped
    assert status.backup is not None
    assert len(status.backup.failed_steps) <= backup.MAX_STEPS
    assert all(len(step) <= backup.MAX_TEXT_LENGTH for step in status.backup.failed_steps)


def write(tmp_path: Path, document: Any) -> str:
    path = tmp_path / "backup-status.json"
    path.write_text(document if isinstance(document, str) else json.dumps(document))
    return str(path)


def test_load_status_reads_a_good_file(tmp_path: Path) -> None:
    assert load_status(write(tmp_path, healthy()), NOW).status == "ok"


def test_not_configured_is_unknown_not_ok() -> None:
    status = load_status("", NOW)

    assert status.status == "unknown"
    assert "SERVER_COPILOT_BACKUP_STATUS_FILE" in status.summary
    assert status.backup is None
    assert status.repositories == []


def test_a_missing_file_is_unknown(tmp_path: Path) -> None:
    status = load_status(str(tmp_path / "nope.json"), NOW)

    assert status.status == "unknown"
    assert "does not exist" in status.summary


def test_a_directory_instead_of_a_file_is_unknown(tmp_path: Path) -> None:
    status = load_status(str(tmp_path), NOW)

    assert status.status == "unknown"
    assert str(tmp_path) not in status.summary


@pytest.mark.parametrize("content", ["", "not json", "[]", "null", '{"schema_version": 1}'])
def test_unusable_content_is_unknown(tmp_path: Path, content: str) -> None:
    status = load_status(write(tmp_path, content), NOW)

    assert status.status == "unknown"


def test_an_unsupported_schema_version_is_unknown(tmp_path: Path) -> None:
    doc = healthy()
    doc["schema_version"] = 2

    status = load_status(write(tmp_path, doc), NOW)

    assert status.status == "unknown"
    assert "Unsupported status schema version 2" in status.summary


def test_a_timestamp_without_a_timezone_is_unknown(tmp_path: Path) -> None:
    doc = copy.deepcopy(healthy())
    doc["generated_at"] = "2026-10-27T11:00:00"

    assert load_status(write(tmp_path, doc), NOW).status == "unknown"


def test_a_huge_file_is_refused(tmp_path: Path) -> None:
    doc = healthy()
    doc["padding"] = "x" * (backup.MAX_FILE_BYTES + 1)

    status = load_status(write(tmp_path, doc), NOW)

    assert status.status == "unknown"
    assert "larger than" in status.summary


def make_client() -> DockerClient:
    return DockerClient(
        "tcp://proxy:2375", transport=httpx2.MockTransport(lambda r: httpx2.Response(200))
    )


async def test_tool_is_registered_read_only_and_reports_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return NOW

    monkeypatch.setattr(backup, "datetime", FrozenDatetime)
    monkeypatch.setenv("SERVER_COPILOT_BACKUP_STATUS_FILE", write(tmp_path, healthy()))
    mcp = create_server(make_client())

    tool = next(t for t in await mcp.list_tools() if t.name == "get_backup_status")
    result = await mcp.call_tool("get_backup_status", {})

    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True
    assert tool.description
    assert isinstance(result, CallToolResult)
    assert result.structured_content is not None
    assert result.structured_content["status"] == "ok"
    assert result.structured_content["checked_at"] == "2026-10-27T12:00:00+00:00"
    assert result.structured_content["backup"]["last_result"] == "ok"
    assert result.structured_content["repositories"][0]["name"] == "drive"


async def test_tool_without_configuration_answers_unknown() -> None:
    mcp = create_server(make_client())

    result = await mcp.call_tool("get_backup_status", {})

    assert isinstance(result, CallToolResult)
    assert result.structured_content is not None
    assert result.structured_content["status"] == "unknown"


async def test_tool_times_out_instead_of_hanging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def slow(path: str, now: datetime) -> backup.BackupStatus:
        time.sleep(0.3)
        return backup.unknown_status("late", now)

    monkeypatch.setattr(backup, "load_status", slow)
    monkeypatch.setattr(backup, "READ_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setenv("SERVER_COPILOT_BACKUP_STATUS_FILE", write(tmp_path, healthy()))
    mcp = create_server(make_client())

    result = await mcp.call_tool("get_backup_status", {})

    assert isinstance(result, CallToolResult)
    assert result.structured_content is not None
    assert result.structured_content["status"] == "unknown"
    assert "timed out" in result.structured_content["summary"]
