"""Ask the host to update a Compose project, without this server ever touching Docker's write API.

The server only drops a small JSON request into a queue folder and reads the result the host
runner leaves in another one. The runner (home_server_infra) owns what may be updated and how:
it validates every request against its own allowlist, runs ``docker compose pull`` and, only if
that works, ``docker compose up -d``.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from server_copilot.compose import ComposeProject, fetch_compose_projects
from server_copilot.docker import DockerClient, DockerError
from server_copilot.masking import mask_url_credentials
from server_copilot.network_snapshot import Detail, clean

# Under uvicorn.error so these lines reach the container log: nothing else configures logging.
logger = logging.getLogger("uvicorn.error.update")

SCHEMA_VERSION = 1
CODE_TTL_SECONDS = 300
CODE_MAC_LENGTH = 24
MAX_PENDING = 5
MAX_SERVICES = 20
MAX_LISTED = 10
MAX_SCANNED = 200
MAX_FILE_BYTES = 64 * 1024
MAX_OUTPUT_CHARS = 16 * 1024
MAX_STEPS = 10
MAX_CONTAINERS = 50
READ_TIMEOUT_SECONDS = 5
STUCK_QUEUE_SECONDS = 180
STUCK_RUN_SECONDS = 30 * 60
VERIFY_SECONDS = 60

NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
REQUEST_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
REQUEST_FILE_PATTERN = re.compile(r"^([0-9a-f]{32})\.json$")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

Status = Literal["queued", "running", "succeeded", "failed", "rejected", "unknown"]
RawStatus = Literal["running", "succeeded", "failed", "rejected"]

# Containers whose restart is worth a warning before anyone confirms.
IMPACT_BY_IMAGE = {
    "cloudflared": "the public Cloudflare tunnel drops while it restarts",
    "docker-socket-proxy": "this server loses its Docker access until the proxy is back",
}


def _clean_output(text: str) -> str:
    """Command output kept as plain text: no colors or control characters, credentials masked."""
    plain = _CONTROL.sub("", _ANSI.sub("", text))
    return mask_url_credentials(plain)[-MAX_OUTPUT_CHARS:]


_Text = Annotated[str, AfterValidator(clean)]
_Output = Annotated[str, AfterValidator(_clean_output)]


class _Raw(BaseModel):
    """What the host writes. Unknown keys are ignored, so they can never reach a response."""

    model_config = ConfigDict(extra="ignore")


class RawRequest(_Raw):
    project: _Text
    services: list[_Text] = Field(default_factory=list)


class RawStep(_Raw):
    name: _Text
    ok: bool
    seconds: float | None = Field(default=None, ge=0)


class RawContainer(_Raw):
    service: _Text
    name: _Text
    state: _Text
    health: _Text | None = None
    image: _Text | None = None


class RawResult(_Raw):
    schema_version: int
    id: str = Field(pattern=REQUEST_ID_PATTERN.pattern)
    project: _Text
    services: list[_Text] = Field(default_factory=list, max_length=MAX_SERVICES)
    requested_at: AwareDatetime
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    status: RawStatus
    failed_step: _Text | None = None
    message: Detail = ""
    steps: list[RawStep] = Field(default_factory=list, max_length=MAX_STEPS)
    containers: list[RawContainer] = Field(default_factory=list, max_length=MAX_CONTAINERS)
    output_tail: _Output = ""


class PlannedService(BaseModel):
    service: str
    image: str
    running: int
    containers: list[str]


class UpdatePlan(BaseModel):
    project: str
    services: list[PlannedService]
    steps: list[str]
    warnings: list[str]
    confirmation_code: str
    expires_at: str
    expires_in_seconds: int


class UpdateRequested(BaseModel):
    request_id: str
    project: str
    services: list[str]
    queued_at: str
    message: str


class UpdateStep(BaseModel):
    name: str
    ok: bool
    seconds: float | None


class UpdateContainer(BaseModel):
    service: str
    name: str
    state: str
    health: str | None
    image: str | None


class UpdateResult(BaseModel):
    request_id: str
    project: str | None
    services: list[str]
    status: Status
    summary: str
    requested_at: str | None
    started_at: str | None
    finished_at: str | None
    failed_step: str | None
    steps: list[UpdateStep]
    containers: list[UpdateContainer]
    output_tail: str
    notes: list[str]


class UpdateStatus(BaseModel):
    updates: list[UpdateResult]
    notes: list[str]


class UpdateError(ToolError):
    """A refusal whose message is safe to show to the MCP client."""


class ConfirmationError(UpdateError):
    pass


def _now() -> float:
    return time.time()


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def _iso_epoch(epoch: float) -> str:
    return _iso(datetime.fromtimestamp(epoch, tz=UTC))


class Confirmations:
    """Short-lived, single-use codes that bind a confirmation to one project and its services.

    The key lives only in this process, so a restart invalidates every code: the plan is simply
    asked for again.
    """

    def __init__(self, ttl: int = CODE_TTL_SECONDS) -> None:
        self._ttl = ttl
        self._key = secrets.token_bytes(32)
        self._used: dict[str, int] = {}

    def _mac(self, project: str, services: tuple[str, ...], expires: int) -> str:
        message = "\0".join([project, ",".join(services), str(expires)]).encode()
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()[:CODE_MAC_LENGTH]

    def issue(self, project: str, services: tuple[str, ...], now: float) -> tuple[str, int]:
        expires = int(now) + self._ttl
        return f"{expires}-{self._mac(project, services, expires)}", expires

    def redeem(self, code: str, project: str, services: tuple[str, ...], now: float) -> None:
        self._used = {c: e for c, e in self._used.items() if e > now}
        expires_text, _, mac = code.strip().partition("-")
        if not expires_text.isdigit() or not mac:
            raise ConfirmationError("That is not a confirmation code; ask plan_compose_update.")
        expires = int(expires_text)
        if not hmac.compare_digest(mac, self._mac(project, services, expires)):
            raise ConfirmationError(
                "The confirmation code does not match this project and these services. "
                "Ask plan_compose_update again."
            )
        if expires <= now:
            raise ConfirmationError("The confirmation code has expired. Ask for a new plan.")
        normalized = code.strip()
        if normalized in self._used:
            raise ConfirmationError("That confirmation code was already used. Ask for a new plan.")
        self._used[normalized] = expires


def _validate_project(project: str) -> str:
    if not NAME_PATTERN.match(project):
        raise UpdateError("project must be a Compose project name such as 'media'.")
    return project


def _normalize_services(services: list[str] | None) -> tuple[str, ...]:
    names = sorted(set(services or []))
    if len(names) > MAX_SERVICES:
        raise UpdateError(f"Name at most {MAX_SERVICES} services, or none for the whole project.")
    for name in names:
        if not NAME_PATTERN.match(name):
            raise UpdateError(f"{name!r} is not a valid service name.")
    return tuple(names)


def _require_enabled(requests_dir: str, results_dir: str) -> None:
    if not requests_dir or not results_dir:
        raise UpdateError(
            "Compose updates are not configured: set SERVER_COPILOT_UPDATE_REQUESTS_DIR "
            "(read-write) and SERVER_COPILOT_UPDATE_RESULTS_DIR (read-only), and run the host "
            "runner that serves them."
        )


# --- the plan -------------------------------------------------------------------------------


def _self_container_id() -> str | None:
    return os.environ.get("HOSTNAME") or None


def _planned(project: ComposeProject, wanted: tuple[str, ...]) -> list[PlannedService]:
    known = {service.name: service for service in project.services}
    unknown = [name for name in wanted if name not in known]
    if unknown:
        raise UpdateError(
            f"Project {project.name!r} has no service {', '.join(map(repr, unknown))}; "
            f"its services are: {', '.join(sorted(known))}."
        )
    chosen = [known[name] for name in wanted] if wanted else list(known.values())
    return [
        PlannedService(
            service=service.name,
            image=service.image,
            running=service.running_count,
            containers=[c.name for c in service.containers],
        )
        for service in chosen
    ]


def _warnings(project: ComposeProject, planned: list[PlannedService]) -> list[str]:
    chosen = {p.service for p in planned}
    warnings = []
    own = _self_container_id()
    containers = [c for s in project.services if s.name in chosen for c in s.containers]
    if own and any(c.id == own[: len(c.id)] for c in containers):
        warnings.append(
            "This MCP server is one of these containers: it restarts during the update and the "
            "connection drops for a few seconds. Read the outcome afterwards with "
            "get_compose_update_status."
        )
    for service in planned:
        for marker, effect in IMPACT_BY_IMAGE.items():
            if marker in service.image:
                warnings.append(f"{service.service}: {effect}.")
    return warnings


def _steps(wanted: tuple[str, ...]) -> list[str]:
    scope = f"the services {', '.join(wanted)}" if wanted else "every service of the project"
    return [
        f"docker compose pull for {scope}: downloads newer images. Nothing is stopped yet.",
        (
            "Only if the pull succeeds: docker compose up -d, which recreates just the "
            "containers whose image or configuration changed. If the pull fails, nothing is "
            "stopped or started."
        ),
        f"Then the container state and health are checked for up to {VERIFY_SECONDS} s.",
        "The host runner updates only the stacks on its own allowlist and rejects any other.",
    ]


# --- the queue ------------------------------------------------------------------------------


def _read_json(path: Path) -> Any | None:
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            return None
        return json.loads(data.decode("utf-8"))
    except (OSError, ValueError):
        return None


def _scan(directory: str) -> list[tuple[str, float]]:
    """(request id, modification time) of the well-named, regular files, newest first."""
    found = []
    try:
        with os.scandir(directory) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_SCANNED:
                    break
                match = REQUEST_FILE_PATTERN.match(entry.name)
                if match and entry.is_file(follow_symlinks=False):
                    found.append((match.group(1), entry.stat(follow_symlinks=False).st_mtime))
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise UpdateError("A queue folder is not mounted in the container.") from exc
    except OSError as exc:
        raise UpdateError(f"A queue folder cannot be read ({exc.strerror}).") from exc
    return sorted(found, key=lambda item: item[1], reverse=True)


def _pending_for(requests_dir: str, results_dir: str, project: str) -> list[str]:
    """Ids of the requests for this project that are queued or still running."""
    busy = []
    for request_id, _ in _scan(requests_dir):
        raw = _read_json(Path(requests_dir) / f"{request_id}.json")
        if isinstance(raw, dict) and raw.get("project") == project:
            busy.append(request_id)
    for request_id, _ in _scan(results_dir)[:MAX_LISTED]:
        result = _load_result(results_dir, request_id)
        if result and result.project == project and result.status == "running":
            busy.append(request_id)
    return busy


def _write_request(requests_dir: str, project: str, services: tuple[str, ...]) -> tuple[str, str]:
    directory = Path(requests_dir)
    if len(_scan(requests_dir)) >= MAX_PENDING:
        raise UpdateError(
            f"{MAX_PENDING} requests are already waiting for the host runner; "
            "check that it is running (get_compose_update_status)."
        )
    request_id = uuid.uuid4().hex
    queued_at = _iso_epoch(_now())
    payload = {
        "schema_version": SCHEMA_VERSION,
        "id": request_id,
        "project": project,
        "services": list(services),
        "requested_at": queued_at,
    }
    temporary = directory / f".{request_id}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(temporary, directory / f"{request_id}.json")
    except OSError as exc:
        raise UpdateError(
            "The request queue is not writable: mount it read-write for this container."
        ) from exc
    return request_id, queued_at


# --- the results ----------------------------------------------------------------------------


def _load_result(results_dir: str, request_id: str) -> RawResult | None:
    raw = _read_json(Path(results_dir) / f"{request_id}.json")
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        return None
    try:
        result = RawResult.model_validate(raw)
    except ValidationError:
        return None
    return result if result.id == request_id else None


def _summary(result: RawResult) -> str:
    if result.message:
        return result.message
    return {
        "running": f"The update of {result.project} is running.",
        "succeeded": f"{result.project} was updated.",
        "failed": f"The update of {result.project} failed"
        + (f" at {result.failed_step}." if result.failed_step else "."),
        "rejected": f"The host runner refused the update of {result.project}.",
    }[result.status]


def _from_raw(result: RawResult, now: float) -> UpdateResult:
    notes = []
    started = result.started_at.timestamp() if result.started_at else None
    if result.status == "running" and started and now - started > STUCK_RUN_SECONDS:
        notes.append(
            "This update has been running for a long time; the host runner may have been "
            "interrupted. Check its service on the host."
        )
    return UpdateResult(
        request_id=result.id,
        project=result.project,
        services=list(result.services),
        status=result.status,
        summary=_summary(result),
        requested_at=_iso(result.requested_at),
        started_at=_iso(result.started_at) if result.started_at else None,
        finished_at=_iso(result.finished_at) if result.finished_at else None,
        failed_step=result.failed_step,
        steps=[UpdateStep(name=s.name, ok=s.ok, seconds=s.seconds) for s in result.steps],
        containers=[
            UpdateContainer(
                service=c.service, name=c.name, state=c.state, health=c.health, image=c.image
            )
            for c in result.containers
        ],
        output_tail=result.output_tail,
        notes=notes,
    )


def _queued(requests_dir: str, request_id: str, mtime: float, now: float) -> UpdateResult:
    raw = _read_json(Path(requests_dir) / f"{request_id}.json")
    try:
        request = RawRequest.model_validate(raw)
    except ValidationError:
        request = None
    waited = max(0, int(now - mtime))
    notes = []
    if waited > STUCK_QUEUE_SECONDS:
        notes.append(
            f"Waiting for {waited} s: the host runner is normally quick to start. "
            "Check that its service is installed and running."
        )
    project = request.project if request else None
    return UpdateResult(
        request_id=request_id,
        project=project,
        services=list(request.services) if request else [],
        status="queued",
        summary=f"Waiting for the host runner ({waited} s)."
        if request
        else "A queued request could not be read.",
        requested_at=_iso_epoch(mtime),
        started_at=None,
        finished_at=None,
        failed_step=None,
        steps=[],
        containers=[],
        output_tail="",
        notes=notes,
    )


def _unknown(request_id: str) -> UpdateResult:
    return UpdateResult(
        request_id=request_id,
        project=None,
        services=[],
        status="unknown",
        summary="No request with that id: it is not queued, and its result is gone or never "
        "existed.",
        requested_at=None,
        started_at=None,
        finished_at=None,
        failed_step=None,
        steps=[],
        containers=[],
        output_tail="",
        notes=[],
    )


def collect_status(
    requests_dir: str, results_dir: str, request_id: str | None, now: float
) -> UpdateStatus:
    queued = _scan(requests_dir)
    if request_id is not None:
        if not REQUEST_ID_PATTERN.match(request_id):
            raise UpdateError("request_id is the 32-character id returned by the update.")
        result = _load_result(results_dir, request_id)
        if result:
            return UpdateStatus(updates=[_from_raw(result, now)], notes=[])
        waiting = [item for item in queued if item[0] == request_id]
        if waiting:
            return UpdateStatus(updates=[_queued(requests_dir, *waiting[0], now)], notes=[])
        return UpdateStatus(updates=[_unknown(request_id)], notes=[])

    updates = [_queued(requests_dir, rid, mtime, now) for rid, mtime in queued]
    unreadable = 0
    results = _scan(results_dir)[:MAX_LISTED]
    for rid, _ in results:
        loaded = _load_result(results_dir, rid)
        if loaded:
            updates.append(_from_raw(loaded, now))
        else:
            unreadable += 1
    notes = [f"{unreadable} result file(s) could not be read."] if unreadable else []
    if not updates:
        notes.append("No update has been requested yet.")
    return UpdateStatus(updates=updates[: MAX_LISTED + MAX_PENDING], notes=notes)


# --- the tools ------------------------------------------------------------------------------


def register_update_tools(
    mcp: MCPServer, docker: DockerClient, requests_dir: str, results_dir: str
) -> None:
    confirmations = Confirmations()

    @mcp.tool(name="plan_compose_update", annotations=ToolAnnotations(read_only_hint=True))
    async def plan_compose_update(
        project: Annotated[
            str, Field(description="Compose project to update, as listed by get_compose_projects.")
        ],
        services: Annotated[
            list[str] | None,
            Field(description="Only these services of the project. Omit for the whole project."),
        ] = None,
    ) -> UpdatePlan:
        """Step 1 of updating a Compose project: show what would happen and get a confirmation code.

        Read-only: nothing is changed. It lists the services and the images they run now, what the
        host will do (pull the images and, only if that works, recreate what changed), and
        warnings such as this server restarting itself. The code it returns is valid for five
        minutes, works once, and only for this project and these services: pass it to
        update_compose_project after the user has agreed to the plan.
        """
        _require_enabled(requests_dir, results_dir)
        name = _validate_project(project)
        wanted = _normalize_services(services)
        try:
            found = (await fetch_compose_projects(docker, project=name)).projects
        except DockerError as exc:
            raise ToolError(str(exc)) from exc
        match = next((p for p in found if p.name == name), None)
        if match is None:
            raise UpdateError(
                f"No Compose project named {name!r} has containers here; see get_compose_projects."
            )
        planned = _planned(match, wanted)
        code, expires = confirmations.issue(name, wanted, _now())
        return UpdatePlan(
            project=name,
            services=planned,
            steps=_steps(wanted),
            warnings=_warnings(match, planned),
            confirmation_code=code,
            expires_at=_iso_epoch(expires),
            expires_in_seconds=CODE_TTL_SECONDS,
        )

    @mcp.tool(
        name="update_compose_project",
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=True,
        ),
    )
    async def update_compose_project(
        project: Annotated[str, Field(description="The same project given to the plan.")],
        confirmation_code: Annotated[
            str, Field(description="The code returned by plan_compose_update.")
        ],
        services: Annotated[
            list[str] | None, Field(description="The same services given to the plan, if any.")
        ] = None,
    ) -> UpdateRequested:
        """Step 2: ask the host to update a Compose project (pull the images, then recreate).

        This changes the server: containers whose image changed are stopped and recreated. Call
        it only after the user has seen the plan from plan_compose_update and agreed. It does
        not run anything itself: it queues a request that the host runner validates against its
        own allowlist and performs, in the background; use get_compose_update_status with the
        returned request_id to follow it. An update the runner cannot do (a pull that fails, a
        stack that is not allowed) leaves the running containers as they were.
        """
        _require_enabled(requests_dir, results_dir)
        name = _validate_project(project)
        wanted = _normalize_services(services)
        try:
            busy = await asyncio.to_thread(_pending_for, requests_dir, results_dir, name)
            if busy:
                raise UpdateError(
                    f"{name} already has an update waiting or running (request {busy[0]}); "
                    "follow it with get_compose_update_status."
                )
            confirmations.redeem(confirmation_code, name, wanted, _now())
            request_id, queued_at = await asyncio.to_thread(
                _write_request, requests_dir, name, wanted
            )
        except ConfirmationError as exc:
            logger.warning("update refused: project=%r: %s", name, exc)
            raise
        logger.info("update requested: id=%s project=%r services=%r", request_id, name, wanted)
        return UpdateRequested(
            request_id=request_id,
            project=name,
            services=list(wanted),
            queued_at=queued_at,
            message=(
                "Queued. The host runner starts within seconds; a pull of large images can take "
                f"minutes. Follow it with get_compose_update_status(request_id={request_id!r})."
            ),
        )

    @mcp.tool(name="get_compose_update_status", annotations=ToolAnnotations(read_only_hint=True))
    async def get_compose_update_status(
        request_id: Annotated[
            str | None,
            Field(
                description="The id returned by update_compose_project. Omit to list recent ones."
            ),
        ] = None,
    ) -> UpdateStatus:
        """Follow Compose updates asked of the host: queued, running, succeeded, failed or rejected.

        Read-only. For each update: the steps with their duration, the state and health of the
        containers afterwards, the tail of the command output (colors and control characters
        removed, credentials masked) and a summary. `queued` for a long time means the host
        runner is not running. Without a request_id it lists the queued and the most recent
        updates.
        """
        _require_enabled(requests_dir, results_dir)
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(collect_status, requests_dir, results_dir, request_id, _now()),
                timeout=READ_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            raise UpdateError("Reading the update queue timed out.") from exc
