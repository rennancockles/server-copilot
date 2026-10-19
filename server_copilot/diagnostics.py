import asyncio
import json
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from server_copilot.containers import ContainerDetails, fetch_container
from server_copilot.docker import DockerClient, DockerError, DockerNotFoundError
from server_copilot.logs import parse_time

EventType = Literal["container", "image", "network", "volume", "daemon"]
ProblemKind = Literal["unhealthy", "restart_loop", "oom_killed", "exited_with_error", "dead"]

DEFAULT_EVENT_LIMIT = 100
MAX_EVENT_LIMIT = 500
MAX_EVENT_BYTES = 4 * 1024 * 1024
DEFAULT_SINCE = "1h"
SHORT_ID_LENGTH = 12
INSPECT_CONCURRENCY = 8
DEFAULT_RESTART_THRESHOLD = 3
RESTART_LOOP_WINDOW_SECONDS = 600


class DockerEvent(BaseModel):
    time: str
    type: str
    action: str
    name: str | None
    id: str | None
    image: str | None
    exit_code: int | None


class RecentEvents(BaseModel):
    events: list[DockerEvent]
    returned: int
    omitted: int
    notes: list[str]


class ProblemContainer(BaseModel):
    id: str
    name: str
    image: str
    problems: list[ProblemKind]
    state: str
    health: str | None
    restart_count: int
    exit_code: int | None
    oom_killed: bool
    finished_at: str | None
    last_health_output: str | None


class ProblemReport(BaseModel):
    problem_containers: list[ProblemContainer]
    checked: int
    errors: list[str]


def _parse_docker_time(value: str | None) -> datetime | None:
    if not value or value.startswith("0001-"):
        return None
    # Docker emits nanoseconds; fromisoformat accepts at most microseconds.
    head, dot, tail = value.rstrip("Z").partition(".")
    text = f"{head}.{tail[:6]}" if dot else head
    try:
        return datetime.fromisoformat(text).replace(tzinfo=UTC)
    except ValueError:
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def to_event(raw: dict[str, Any]) -> DockerEvent:
    actor = raw.get("Actor") or {}
    attributes: dict[str, str] = actor.get("Attributes") or {}
    action = str(raw.get("Action", ""))
    if action.startswith("exec_"):
        # "exec_start: <command>" would leak the command line.
        action = action.partition(":")[0]
    epoch = raw.get("time")
    return DockerEvent(
        time=datetime.fromtimestamp(epoch, tz=UTC).isoformat() if epoch else "",
        type=str(raw.get("Type", "")),
        action=action,
        name=attributes.get("name"),
        id=(actor.get("ID") or "")[:SHORT_ID_LENGTH] or None,
        image=attributes.get("image"),
        exit_code=_int_or_none(attributes.get("exitCode")),
    )


def parse_events(data: bytes) -> list[DockerEvent]:
    events = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        try:
            raw = json.loads(line)
        except ValueError:
            continue  # last line may be cut off by the byte cap
        if isinstance(raw, dict):
            events.append(to_event(raw))
    return events


async def fetch_recent_events(  # noqa: PLR0913
    docker: DockerClient,
    *,
    since: str = DEFAULT_SINCE,
    until: str | None = None,
    event_type: EventType = "container",
    container: str | None = None,
    actions: list[str] | None = None,
    limit: int = DEFAULT_EVENT_LIMIT,
    now: datetime | None = None,
) -> RecentEvents:
    # `until` is required: without it Docker keeps the stream open forever.
    end = parse_time(until, now) if until else int((now or datetime.now(UTC)).timestamp())
    filters: dict[str, list[str]] = {"type": [event_type]}
    if container:
        filters["container"] = [container]
    if actions:
        filters["event"] = actions
    data, capped = await docker.get_bytes(
        "/events",
        {"since": parse_time(since, now), "until": end, "filters": json.dumps(filters)},
        max_bytes=MAX_EVENT_BYTES,
    )
    events = parse_events(data)
    omitted = max(len(events) - limit, 0)
    notes = []
    if omitted:
        notes.append(f"Showing the newest {limit} events; {omitted} older ones omitted.")
    if capped:
        notes.append("The event stream was cut at 4 MiB; narrow the window or add filters.")
    return RecentEvents(
        events=events[-limit:], returned=min(len(events), limit), omitted=omitted, notes=notes
    )


def classify(
    details: ContainerDetails,
    *,
    restart_threshold: int = DEFAULT_RESTART_THRESHOLD,
    now: datetime | None = None,
) -> list[ProblemKind]:
    state = details.state
    now = now or datetime.now(UTC)
    problems: list[ProblemKind] = []
    if details.health and details.health.status == "unhealthy":
        problems.append("unhealthy")
    started = _parse_docker_time(state.started_at)
    recently_started = (
        started is not None and (now - started).total_seconds() < RESTART_LOOP_WINDOW_SECONDS
    )
    # RestartCount is cumulative, so a high count only means a loop if it is still happening.
    if state.restarting or (
        details.restart_count >= restart_threshold and state.running and recently_started
    ):
        problems.append("restart_loop")
    if state.oom_killed:
        problems.append("oom_killed")
    if state.status == "dead":
        problems.append("dead")
    elif state.status == "exited" and state.exit_code not in (0, None):
        problems.append("exited_with_error")
    return problems


async def find_problems(
    docker: DockerClient,
    *,
    restart_threshold: int = DEFAULT_RESTART_THRESHOLD,
    now: datetime | None = None,
) -> ProblemReport:
    listed = await docker.get_json("/containers/json", {"all": "true"})
    semaphore = asyncio.Semaphore(INSPECT_CONCURRENCY)
    errors: list[str] = []

    async def inspect(item: dict[str, Any]) -> ContainerDetails | None:
        async with semaphore:
            try:
                return await fetch_container(docker, item["Id"])
            except DockerNotFoundError:
                return None  # removed between list and inspect
            except DockerError as exc:
                name = (item.get("Names") or [item["Id"]])[0].lstrip("/")
                errors.append(f"{name}: {exc}")
                return None

    inspected = [d for d in await asyncio.gather(*(inspect(i) for i in listed)) if d]
    found = []
    for details in inspected:
        problems = classify(details, restart_threshold=restart_threshold, now=now)
        if problems:
            found.append(
                ProblemContainer(
                    id=details.id,
                    name=details.name,
                    image=details.image,
                    problems=problems,
                    state=details.state.status,
                    health=details.health.status if details.health else None,
                    restart_count=details.restart_count,
                    exit_code=details.state.exit_code,
                    oom_killed=details.state.oom_killed,
                    finished_at=details.state.finished_at,
                    last_health_output=details.health.last_output if details.health else None,
                )
            )
    found.sort(key=lambda c: (-len(c.problems), c.name))
    return ProblemReport(problem_containers=found, checked=len(inspected), errors=errors)


def register_diagnostic_tools(mcp: MCPServer, docker: DockerClient) -> None:
    read_only = ToolAnnotations(read_only_hint=True)

    @mcp.tool(annotations=read_only)
    async def recent_events(  # noqa: PLR0913, PLR0917
        since: Annotated[
            str,
            Field(
                description="Start of the window: relative ('30m', '2h', '1d') "
                "or ISO 8601 (UTC if no timezone)."
            ),
        ] = DEFAULT_SINCE,
        until: Annotated[
            str | None, Field(description="End of the window (same formats); defaults to now.")
        ] = None,
        event_type: Annotated[
            EventType, Field(description="Kind of object to watch.")
        ] = "container",
        container: Annotated[
            str | None, Field(description="Only events of this container name or ID.")
        ] = None,
        actions: Annotated[
            list[str] | None,
            Field(
                description="Only these actions, e.g. ['die', 'oom', 'restart', 'start', "
                "'health_status: unhealthy']."
            ),
        ] = None,
        limit: Annotated[
            int,
            Field(ge=1, le=MAX_EVENT_LIMIT, description="Maximum number of newest events."),
        ] = DEFAULT_EVENT_LIMIT,
    ) -> RecentEvents:
        """List Docker events (start, die, oom, restart, health_status, ...) in a time window.

        Oldest first; 'die' events carry the exit code. Needs EVENTS=1 on the proxy.
        """
        try:
            return await fetch_recent_events(
                docker,
                since=since,
                until=until,
                event_type=event_type,
                container=container,
                actions=actions,
                limit=limit,
            )
        except DockerError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool(annotations=read_only)
    async def find_problem_containers(
        restart_threshold: Annotated[
            int,
            Field(
                ge=1,
                description="Restarts needed to flag a restart loop for a container that "
                "started in the last 10 minutes (a container restarting right now is always "
                "flagged).",
            ),
        ] = DEFAULT_RESTART_THRESHOLD,
    ) -> ProblemReport:
        """Find containers that are unhealthy, restart looping, OOM killed, dead or crashed.

        Checks every container, including stopped ones. Containers that exited with code 0 are
        not reported. oom_killed reflects the container's last exit.
        """
        try:
            return await find_problems(docker, restart_threshold=restart_threshold)
        except DockerError as exc:
            raise ToolError(str(exc)) from exc
