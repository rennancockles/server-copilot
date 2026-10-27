import asyncio
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

SCHEMA_VERSION = 1
MAX_FILE_BYTES = 64 * 1024
READ_TIMEOUT_SECONDS = 5
DEFAULT_MAX_BACKUP_AGE_HOURS = 26
# The repository check runs weekly and the restore test monthly; each gets some slack.
INTEGRITY_MAX_AGE_HOURS = 9 * 24
RESTORE_TEST_MAX_AGE_HOURS = 35 * 24
SECONDS_PER_HOUR = 3600
HOURS_PER_DAY = 24
HOUR_DIGITS = 1
MAX_STEPS = 10
MAX_TEXT_LENGTH = 60

Result = Literal["ok", "failed"]
CheckState = Literal["ok", "warning", "error"]
OverallStatus = Literal["ok", "warning", "error", "unknown"]


class _Raw(BaseModel):
    """What the host writes. Unknown keys are ignored, so they can never reach a response."""

    model_config = ConfigDict(extra="ignore")


class RawRun(_Raw):
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    duration_seconds: int | None = Field(default=None, ge=0)
    result: Result | None = None
    failed_steps: list[str] = Field(default_factory=list)


class RawRestoreRun(RawRun):
    next_run_at: AwareDatetime | None = None


class RawRepository(_Raw):
    newest_snapshot_at: AwareDatetime | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    free_bytes: int | None = Field(default=None, ge=0)


class RawBackup(_Raw):
    last_run: RawRun | None = None
    last_success_at: AwareDatetime | None = None
    repositories: dict[str, RawRepository] = Field(default_factory=dict)


class RawStatusFile(_Raw):
    schema_version: int
    generated_at: AwareDatetime
    max_backup_age_hours: int = Field(default=DEFAULT_MAX_BACKUP_AGE_HOURS, gt=0)
    backup: RawBackup = Field(default_factory=RawBackup)
    integrity: RawRun | None = None
    restore_test: RawRestoreRun | None = None


class Check(BaseModel):
    state: CheckState
    detail: str
    last_result: Result | None
    finished_at: str | None
    age_hours: float | None
    duration_seconds: int | None
    failed_steps: list[str]


class BackupCheck(Check):
    last_success_at: str | None
    last_success_age_hours: float | None
    max_age_hours: int


class RestoreCheck(Check):
    next_run_at: str | None


class Repository(BaseModel):
    name: str
    newest_snapshot_at: str | None
    snapshot_age_hours: float | None
    size_bytes: int | None
    free_bytes: int | None


class BackupStatus(BaseModel):
    status: OverallStatus
    summary: str
    problems: list[str]
    checked_at: str
    status_file_generated_at: str | None
    status_file_age_hours: float | None
    backup: BackupCheck | None
    integrity: Check | None
    restore_test: RestoreCheck | None
    repositories: list[Repository]


_UNSAFE_TEXT = re.compile(r"[^A-Za-z0-9 _.-]")


def _clean(text: str) -> str:
    return _UNSAFE_TEXT.sub("", text).strip()[:MAX_TEXT_LENGTH]


def _clean_steps(steps: list[str]) -> list[str]:
    return [step for step in (_clean(s) for s in steps[:MAX_STEPS]) if step]


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _iso_or_none(moment: datetime | None) -> str | None:
    return _iso(moment) if moment else None


def _hours(now: datetime, moment: datetime) -> float:
    return round(max(0.0, (now - moment).total_seconds() / SECONDS_PER_HOUR), HOUR_DIGITS)


def _hours_or_none(now: datetime, moment: datetime | None) -> float | None:
    return _hours(now, moment) if moment else None


def _age_text(hours: float) -> str:
    if hours < HOURS_PER_DAY * 2:
        return f"{hours:g} h"
    return f"{round(hours / HOURS_PER_DAY)} days"


def _run_fields(run: RawRun | None, now: datetime) -> dict[str, Any]:
    if run is None:
        return {
            "last_result": None,
            "finished_at": None,
            "age_hours": None,
            "duration_seconds": None,
            "failed_steps": [],
        }
    return {
        "last_result": run.result,
        "finished_at": _iso_or_none(run.finished_at),
        "age_hours": _hours_or_none(now, run.finished_at),
        "duration_seconds": run.duration_seconds,
        "failed_steps": _clean_steps(run.failed_steps),
    }


def _repository(name: str, raw: RawRepository, now: datetime) -> Repository:
    return Repository(
        name=_clean(name),
        newest_snapshot_at=_iso_or_none(raw.newest_snapshot_at),
        snapshot_age_hours=_hours_or_none(now, raw.newest_snapshot_at),
        size_bytes=raw.size_bytes,
        free_bytes=raw.free_bytes,
    )


def _repository_problem(name: str, raw: RawRepository, max_age: int, now: datetime) -> str | None:
    label = _clean(name)
    if raw.newest_snapshot_at is None:
        return f"The {label} repository has no readable snapshot."
    age = _hours(now, raw.newest_snapshot_at)
    if age > max_age:
        return f"The newest {label} snapshot is {_age_text(age)} old (limit {max_age} h)."
    return None


def _backup_problems(backup: RawBackup, max_age: int, now: datetime) -> list[str]:
    problems: list[str] = []
    run = backup.last_run
    if run is None or run.result is None:
        problems.append("No backup run has been recorded.")
    elif run.result == "failed":
        steps = ", ".join(_clean_steps(run.failed_steps)) or "unknown step"
        problems.append(f"The last backup run failed ({steps}).")

    if backup.last_success_at is None:
        problems.append("No successful backup is recorded.")
    else:
        age = _hours(now, backup.last_success_at)
        if age > max_age:
            problems.append(
                f"The last successful backup is {_age_text(age)} old (limit {max_age} h)."
            )

    if not backup.repositories:
        problems.append("The status lists no backup repository.")
    for name, repository in sorted(backup.repositories.items()):
        if problem := _repository_problem(name, repository, max_age, now):
            problems.append(problem)
    return problems


def _backup_check(
    backup: RawBackup, problems: list[str], max_age: int, now: datetime
) -> BackupCheck:
    state: CheckState = "error" if problems else "ok"
    detail = (
        " ".join(problems) or "The last run succeeded and every repository has a recent snapshot."
    )
    return BackupCheck(
        state=state,
        detail=detail,
        last_success_at=_iso_or_none(backup.last_success_at),
        last_success_age_hours=_hours_or_none(now, backup.last_success_at),
        max_age_hours=max_age,
        **_run_fields(backup.last_run, now),
    )


def _periodic_state(
    label: str, run: RawRun | None, max_hours: int, now: datetime
) -> tuple[CheckState, str]:
    if run is None or run.result is None or run.finished_at is None:
        return "warning", f"No {label} has been recorded yet."
    if run.result == "failed":
        return "error", f"The last {label} failed."
    age = _hours(now, run.finished_at)
    if age > max_hours:
        return "warning", f"The last {label} was {_age_text(age)} ago, longer than expected."
    return "ok", f"The last {label} passed {_age_text(age)} ago."


def _integrity_check(run: RawRun | None, now: datetime) -> Check:
    state, detail = _periodic_state("repository integrity check", run, INTEGRITY_MAX_AGE_HOURS, now)
    return Check(state=state, detail=detail, **_run_fields(run, now))


def _restore_check(run: RawRestoreRun | None, now: datetime) -> RestoreCheck:
    state, detail = _periodic_state("restore test", run, RESTORE_TEST_MAX_AGE_HOURS, now)
    return RestoreCheck(
        state=state,
        detail=detail,
        next_run_at=_iso_or_none(run.next_run_at if run else None),
        **_run_fields(run, now),
    )


def _overall(states: set[CheckState], *, stale: bool) -> OverallStatus:
    if "error" in states:
        return "error"
    if "warning" in states or stale:
        return "warning"
    return "ok"


def _summary(problems: list[str], backup: BackupCheck) -> str:
    if not problems:
        age = backup.last_success_age_hours or 0.0
        return (
            f"All checks passed: the last successful backup is {_age_text(age)} old, "
            "and the integrity check and restore test are current."
        )
    extra = f" (+{len(problems) - 1} more)" if len(problems) > 1 else ""
    return f"{problems[0]}{extra}"


def evaluate(raw: RawStatusFile, now: datetime) -> BackupStatus:
    max_age = raw.max_backup_age_hours
    backup_problems = _backup_problems(raw.backup, max_age, now)
    backup = _backup_check(raw.backup, backup_problems, max_age, now)
    integrity = _integrity_check(raw.integrity, now)
    restore = _restore_check(raw.restore_test, now)

    file_age = _hours(now, raw.generated_at)
    stale = file_age > max_age
    problems = list(backup_problems)
    problems += [c.detail for c in (integrity, restore) if c.state != "ok"]
    if stale:
        problems.append(f"The status file was last refreshed {_age_text(file_age)} ago.")

    return BackupStatus(
        status=_overall({backup.state, integrity.state, restore.state}, stale=stale),
        summary=_summary(problems, backup),
        problems=problems,
        checked_at=_iso(now),
        status_file_generated_at=_iso(raw.generated_at),
        status_file_age_hours=file_age,
        backup=backup,
        integrity=integrity,
        restore_test=restore,
        repositories=[_repository(n, r, now) for n, r in sorted(raw.backup.repositories.items())],
    )


def unknown_status(reason: str, now: datetime) -> BackupStatus:
    """No usable data: the answer is "unknown", never "ok"."""
    return BackupStatus(
        status="unknown",
        summary=reason,
        problems=[reason],
        checked_at=_iso(now),
        status_file_generated_at=None,
        status_file_age_hours=None,
        backup=None,
        integrity=None,
        restore_test=None,
        repositories=[],
    )


def _read_status_text(path: str) -> str:
    with Path(path).open("rb") as handle:
        data = handle.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"The status file is larger than {MAX_FILE_BYTES // 1024} KiB.")
    return data.decode("utf-8")


def _parse(document: Any) -> RawStatusFile | str:
    """The parsed status, or the reason it cannot be used."""
    version = document.get("schema_version") if isinstance(document, dict) else None
    if version != SCHEMA_VERSION:
        shown = _clean(str(version))
        return f"Unsupported status schema version {shown} (expected {SCHEMA_VERSION})."
    try:
        return RawStatusFile.model_validate(document)
    except ValidationError:
        return "The backup status file has an unexpected format."


def load_status(path: str, now: datetime) -> BackupStatus:
    if not path:
        return unknown_status(
            "Backup status is not configured: set SERVER_COPILOT_BACKUP_STATUS_FILE and mount the "
            "status folder read-only.",
            now,
        )
    try:
        document = json.loads(_read_status_text(path))
    except FileNotFoundError:
        return unknown_status("The backup status file does not exist yet.", now)
    except OSError as exc:
        return unknown_status(f"The backup status file cannot be read ({exc.strerror}).", now)
    except ValueError as exc:
        return unknown_status(f"The backup status file is not usable: {exc}", now)

    parsed = _parse(document)
    if isinstance(parsed, str):
        return unknown_status(parsed, now)
    return evaluate(parsed, now)


def register_backup_tools(mcp: MCPServer, status_file: str) -> None:
    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def get_backup_status() -> BackupStatus:
        """Tell whether the home server backups ran, are recent and can be recovered.

        Reads the small status file the host writes after each backup, repository check and
        restore test; nothing is run and nothing is changed. Three checks are reported
        separately: `backup` (last run, its duration and result, and the age of the newest
        Restic snapshot in the local and Google Drive repositories, plus the Drive size and free
        space), `integrity` (the periodic `restic check`) and `restore_test` (the monthly
        restore test, with the next scheduled run when known). `status` is ok, warning, error
        or unknown: unknown means there is no usable data, so it must not be read as healthy.
        Ages are in hours and every timestamp carries its timezone. Failed steps are step
        names only, never logs, paths or credentials.
        """
        now = datetime.now(UTC)
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(load_status, status_file, now), timeout=READ_TIMEOUT_SECONDS
            )
        except TimeoutError:
            return unknown_status("Reading the backup status file timed out.", now)
