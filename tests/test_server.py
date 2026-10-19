import base64
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import uvicorn
from mcp.types import CallToolResult, TextContent
from starlette.testclient import TestClient

from server_copilot.__main__ import main
from server_copilot.config import Settings, get_settings
from server_copilot.server import create_server

pytestmark = pytest.mark.unit


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
    assert "ping" in {t.name for t in tools}
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


def test_main_serves_app_with_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVER_COPILOT_HOST", "127.0.0.1")
    monkeypatch.setenv("SERVER_COPILOT_PORT", "9001")
    monkeypatch.setenv("SERVER_COPILOT_AUTH_DISABLED", "true")
    run = MagicMock()
    monkeypatch.setattr(uvicorn, "run", run)

    main()

    run.assert_called_once()
    assert run.call_args.kwargs == {"host": "127.0.0.1", "port": 9001}


def test_main_exits_when_token_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SERVER_COPILOT_AUTH_TOKEN", raising=False)
    run = MagicMock()
    monkeypatch.setattr(uvicorn, "run", run)

    with pytest.raises(SystemExit, match="SERVER_COPILOT_AUTH_TOKEN"):
        main()

    run.assert_not_called()


def test_health_endpoint() -> None:
    app = create_server().streamable_http_app(host=Settings().host)
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_server_advertises_its_icon() -> None:
    icons = create_server().icons

    assert icons is not None
    assert len(icons) == 1
    assert icons[0].mime_type == "image/png"
    assert icons[0].sizes == ["128x128"]
    prefix = "data:image/png;base64,"
    assert icons[0].src.startswith(prefix)
    assert base64.b64decode(icons[0].src[len(prefix) :]).startswith(b"\x89PNG")


def test_initialize_response_includes_icon() -> None:
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

    assert "data:image/png;base64," in response.text
