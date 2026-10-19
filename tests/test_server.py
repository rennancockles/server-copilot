from pathlib import Path
from unittest.mock import MagicMock

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent
from starlette.testclient import TestClient

from server_copilot.__main__ import main
from server_copilot.config import Settings, get_settings
from server_copilot.server import create_server


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


def test_settings_defaults() -> None:
    settings = Settings()
    assert settings.port == 8000
    assert settings.docker_host == "tcp://docker-socket-proxy:2375"


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVER_COPILOT_PORT", "9000")
    monkeypatch.setenv("SERVER_COPILOT_DOCKER_HOST", "tcp://proxy:2375")
    settings = Settings()
    assert settings.port == 9000
    assert settings.docker_host == "tcp://proxy:2375"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_ping_tool() -> None:
    mcp = create_server()
    tools = await mcp.list_tools()
    assert [t.name for t in tools] == ["ping"]
    result = await mcp.call_tool("ping", {})
    assert isinstance(result, CallToolResult)
    content = result.content[0]
    assert isinstance(content, TextContent)
    assert content.text == "ok"


def test_streamable_http_initialize() -> None:
    app = create_server().streamable_http_app(host=Settings().host)
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            },
        )
    assert response.status_code == 200
    assert "server-copilot" in response.text


def test_get_settings_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVER_COPILOT_HOST", "127.0.0.1")
    assert get_settings().host == "127.0.0.1"


def test_main_runs_streamable_http(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVER_COPILOT_HOST", "127.0.0.1")
    monkeypatch.setenv("SERVER_COPILOT_PORT", "9001")
    run = MagicMock()
    monkeypatch.setattr(MCPServer, "run", run)

    main()

    run.assert_called_once_with("streamable-http", host="127.0.0.1", port=9001)
