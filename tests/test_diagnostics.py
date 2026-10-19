import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx2
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult

from server_copilot import diagnostics
from server_copilot.containers import to_details
from server_copilot.diagnostics import (
    ProblemKind,
    classify,
    fetch_recent_events,
    find_problems,
    parse_events,
)
from server_copilot.docker import DockerClient
from server_copilot.server import create_server

pytestmark = pytest.mark.unit

Handler = Callable[[httpx2.Request], httpx2.Response]

NOW = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)
NOW_EPOCH = int(NOW.timestamp())


def make_client(handler: Handler) -> DockerClient:
    return DockerClient("tcp://proxy:2375", transport=httpx2.MockTransport(handler))


def event(action: str, name: str = "app", offset: int = 0, **attrs: str) -> dict[str, Any]:
    return {
        "Type": "container",
        "Action": action,
        "Actor": {"ID": "a" * 64, "Attributes": {"name": name, "image": "app:1", **attrs}},
        "time": NOW_EPOCH - 600 + offset,
    }


def ndjson(*events: dict[str, Any]) -> bytes:
    return b"".join(json.dumps(e).encode() + b"\n" for e in events)


def inspect_payload(  # noqa: PLR0913
    name: str,
    *,
    status: str = "running",
    exit_code: int = 0,
    oom: bool = False,
    restarting: bool = False,
    restart_count: int = 0,
    started_seconds_ago: int = 86400,
    health: str | None = None,
) -> dict[str, Any]:
    started = datetime.fromtimestamp(NOW_EPOCH - started_seconds_ago, tz=UTC)
    state: dict[str, Any] = {
        "Status": status,
        "Running": status == "running",
        "Restarting": restarting,
        "OOMKilled": oom,
        "ExitCode": exit_code,
        "StartedAt": started.strftime("%Y-%m-%dT%H:%M:%S.123456789Z"),
        "FinishedAt": "2026-10-19T11:59:00Z",
    }
    if health:
        state["Health"] = {"Status": health, "FailingStreak": 5, "Log": [{"Output": "refused\n"}]}
    return {
        "Id": name.ljust(64, "0"),
        "Name": f"/{name}",
        "RestartCount": restart_count,
        "Config": {"Image": f"{name}:1"},
        "State": state,
    }


def classify_payload(**kwargs: Any) -> list[ProblemKind]:
    return classify(to_details(inspect_payload("app", **kwargs)), now=NOW)


def test_parse_events_maps_fields_and_skips_garbage() -> None:
    data = ndjson(event("die", exitCode="137")) + b"not json\n" + b'{"Type": "container", "Act'

    events = parse_events(data)

    assert len(events) == 1
    assert events[0].action == "die"
    assert events[0].name == "app"
    assert events[0].id == "a" * 12
    assert events[0].image == "app:1"
    assert events[0].exit_code == 137
    assert events[0].time == "2026-10-19T11:50:00+00:00"


def test_exec_command_is_not_exposed() -> None:
    events = parse_events(ndjson(event("exec_start: sh -c 'curl -H token=hunter2 x'")))

    assert events[0].action == "exec_start"
    assert "hunter2" not in events[0].model_dump_json()


def test_health_status_action_keeps_its_detail() -> None:
    assert parse_events(ndjson(event("health_status: unhealthy")))[0].action == (
        "health_status: unhealthy"
    )


async def test_recent_events_sends_window_and_filters() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/events"
        seen.update(request.url.params)
        return httpx2.Response(200, content=ndjson(event("die"), event("start", offset=5)))

    async with make_client(handler) as docker:
        result = await fetch_recent_events(
            docker, since="1h", container="app", actions=["die", "start"], now=NOW
        )

    assert seen["since"] == str(NOW_EPOCH - 3600)
    assert seen["until"] == str(NOW_EPOCH)  # always bounded, otherwise the stream never ends
    assert json.loads(seen["filters"]) == {
        "type": ["container"],
        "container": ["app"],
        "event": ["die", "start"],
    }
    assert [e.action for e in result.events] == ["die", "start"]
    assert result.omitted == 0
    assert result.notes == []


async def test_recent_events_explicit_until_and_type() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.update(request.url.params)
        return httpx2.Response(200, content=b"")

    async with make_client(handler) as docker:
        result = await fetch_recent_events(
            docker, since="2h", until="30m", event_type="network", now=NOW
        )

    assert seen["until"] == str(NOW_EPOCH - 1800)
    assert json.loads(seen["filters"]) == {"type": ["network"]}
    assert result.events == []


async def test_recent_events_keeps_newest_when_over_limit() -> None:
    body = ndjson(*(event("start", offset=n) for n in range(10)))
    async with make_client(lambda r: httpx2.Response(200, content=body)) as docker:
        result = await fetch_recent_events(docker, limit=3, now=NOW)

    assert len(result.events) == 3
    assert result.events[-1].time == "2026-10-19T11:50:09+00:00"
    assert result.omitted == 7
    assert result.returned == 3
    assert "7 older" in result.notes[0]


async def test_recent_events_notes_byte_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diagnostics, "MAX_EVENT_BYTES", 300)
    body = ndjson(*(event("start", offset=n) for n in range(20)))
    async with make_client(lambda r: httpx2.Response(200, content=body)) as docker:
        result = await fetch_recent_events(docker, now=NOW)

    assert any("cut at 4 MiB" in note for note in result.notes)
    assert 0 < len(result.events) < 20


async def test_recent_events_blocked_by_proxy_names_permission() -> None:
    async with make_client(lambda r: httpx2.Response(403)) as docker:
        with pytest.raises(diagnostics.DockerError, match=r"EVENTS=1"):
            await fetch_recent_events(docker, now=NOW)


def test_healthy_running_container_has_no_problems() -> None:
    assert classify_payload(health="healthy") == []
    assert classify_payload() == []


def test_unhealthy() -> None:
    assert classify_payload(health="unhealthy") == ["unhealthy"]


def test_oom_killed() -> None:
    assert classify_payload(status="exited", exit_code=137, oom=True) == [
        "oom_killed",
        "exited_with_error",
    ]


def test_exited_with_error_vs_clean_exit() -> None:
    assert classify_payload(status="exited", exit_code=1) == ["exited_with_error"]
    assert classify_payload(status="exited", exit_code=0) == []


def test_dead() -> None:
    assert classify_payload(status="dead", exit_code=1) == ["dead"]


def test_restart_loop_when_restarting_now() -> None:
    assert classify_payload(status="restarting", restarting=True, restart_count=1) == [
        "restart_loop"
    ]


def test_restart_loop_when_many_restarts_and_just_started() -> None:
    assert classify_payload(restart_count=5, started_seconds_ago=30) == ["restart_loop"]


def test_old_restarts_on_a_stable_container_are_not_a_loop() -> None:
    assert classify_payload(restart_count=40, started_seconds_ago=7 * 86400) == []


def test_few_restarts_are_not_a_loop() -> None:
    assert classify_payload(restart_count=2, started_seconds_ago=30) == []


def test_restart_threshold_is_configurable() -> None:
    details = to_details(inspect_payload("app", restart_count=2, started_seconds_ago=30))

    assert classify(details, restart_threshold=2, now=NOW) == ["restart_loop"]


def test_containers_with_unparseable_start_time_are_not_flagged_as_loops() -> None:
    payload = inspect_payload("app", restart_count=9)
    payload["State"]["StartedAt"] = "0001-01-01T00:00:00Z"

    assert classify(to_details(payload), now=NOW) == []


def problem_handler(request: httpx2.Request) -> httpx2.Response:
    path = request.url.path
    if path == "/containers/json":
        assert request.url.params["all"] == "true"
        names = ["good", "sick", "crashed", "gone", "broken"]
        return httpx2.Response(
            200, json=[{"Id": n.ljust(64, "0"), "Names": [f"/{n}"]} for n in names]
        )
    payloads = {
        "good": inspect_payload("good", health="healthy"),
        "sick": inspect_payload("sick", health="unhealthy"),
        "crashed": inspect_payload("crashed", status="exited", exit_code=137, oom=True),
    }
    for name, payload in payloads.items():
        if path == f"/containers/{name.ljust(64, '0')}/json":
            return httpx2.Response(200, json=payload)
    if "gone" in path:
        return httpx2.Response(404, json={"message": "No such container"})
    return httpx2.Response(500, json={"message": "inspect failed"})


async def test_find_problems_reports_only_problem_containers() -> None:
    async with make_client(problem_handler) as docker:
        report = await find_problems(docker, now=NOW)

    assert report.checked == 3  # "gone" vanished, "broken" errored
    assert [c.name for c in report.problem_containers] == ["crashed", "sick"]
    crashed, sick = report.problem_containers
    assert crashed.problems == ["oom_killed", "exited_with_error"]
    assert crashed.exit_code == 137
    assert crashed.oom_killed is True
    assert sick.problems == ["unhealthy"]
    assert sick.health == "unhealthy"
    assert sick.last_health_output == "refused"
    assert len(report.errors) == 1
    assert report.errors[0].startswith("broken:")


async def test_find_problems_with_nothing_wrong() -> None:
    async with make_client(lambda r: httpx2.Response(200, json=[])) as docker:
        report = await find_problems(docker, now=NOW)

    assert report.problem_containers == []
    assert report.checked == 0


async def test_tools_registered_and_structured() -> None:
    mcp = create_server(make_client(problem_handler))
    tools = {t.name: t for t in await mcp.list_tools()}

    assert {"recent_events", "find_problem_containers"} <= tools.keys()
    properties = tools["recent_events"].input_schema["properties"]
    assert set(properties) == {"since", "until", "event_type", "container", "actions", "limit"}
    assert all(prop.get("description") for prop in properties.values())
    assert properties["limit"]["maximum"] == diagnostics.MAX_EVENT_LIMIT
    for name in ("recent_events", "find_problem_containers"):
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.read_only_hint is True

    result = await mcp.call_tool("find_problem_containers", {})

    assert isinstance(result, CallToolResult)
    assert result.structured_content is not None
    assert result.structured_content["problem_containers"][0]["name"] == "crashed"


async def test_recent_events_tool_returns_structured_events() -> None:
    body = ndjson(event("die", exitCode="1"))
    mcp = create_server(make_client(lambda r: httpx2.Response(200, content=body)))

    result = await mcp.call_tool("recent_events", {"since": "1h"})

    assert isinstance(result, CallToolResult)
    assert result.structured_content is not None
    assert result.structured_content["events"][0]["exit_code"] == 1


async def test_tools_surface_errors_as_tool_errors() -> None:
    mcp = create_server(make_client(lambda r: httpx2.Response(403)))

    with pytest.raises(ToolError, match=r"EVENTS=1"):
        await mcp.call_tool("recent_events", {})
    with pytest.raises(ToolError, match=r"CONTAINERS=1"):
        await mcp.call_tool("find_problem_containers", {})
    with pytest.raises(ToolError, match="Invalid time"):
        await mcp.call_tool("recent_events", {"since": "soon"})
