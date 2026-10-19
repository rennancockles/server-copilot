import logging

import pytest
from pydantic import SecretStr
from starlette.testclient import TestClient

from server_copilot.app import create_app
from server_copilot.auth import MIN_TOKEN_LENGTH, AuthConfigError, resolve_token
from server_copilot.config import Settings

pytestmark = pytest.mark.unit

TOKEN = "t0ken-" + "x" * 40
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


def make_settings() -> Settings:
    return Settings(auth_token=SecretStr(TOKEN))


def test_missing_token_refuses_to_start() -> None:
    with pytest.raises(AuthConfigError, match="SERVER_COPILOT_AUTH_TOKEN is not set"):
        create_app(Settings())


def test_short_token_is_rejected() -> None:
    with pytest.raises(AuthConfigError, match=str(MIN_TOKEN_LENGTH)):
        resolve_token("short", auth_disabled=False)


def test_auth_can_be_disabled_explicitly() -> None:
    assert resolve_token(None, auth_disabled=True) is None
    with TestClient(create_app(Settings(auth_disabled=True))) as client:
        assert client.post("/mcp", headers=MCP_HEADERS, json=INITIALIZE).status_code == 200


def test_request_without_token_gets_401() -> None:
    with TestClient(create_app(make_settings())) as client:
        response = client.post("/mcp", headers=MCP_HEADERS, json=INITIALIZE)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"error": "unauthorized"}


@pytest.mark.parametrize(
    "authorization",
    [
        "Bearer wrong-token",
        f"Bearer {TOKEN}x",
        f"Bearer {TOKEN[:-1]}",
        "Bearer",
        "Bearer ",
        TOKEN,
        f"Basic {TOKEN}",
        f"Token {TOKEN}",
    ],
)
def test_invalid_credentials_get_401(authorization: str) -> None:
    with TestClient(create_app(make_settings())) as client:
        response = client.post(
            "/mcp", headers={**MCP_HEADERS, "Authorization": authorization}, json=INITIALIZE
        )

    assert response.status_code == 401


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER"])
def test_valid_token_is_accepted(scheme: str) -> None:
    with TestClient(create_app(make_settings())) as client:
        response = client.post(
            "/mcp",
            headers={**MCP_HEADERS, "Authorization": f"{scheme} {TOKEN}"},
            json=INITIALIZE,
        )

    assert response.status_code == 200
    assert "server-copilot" in response.text


def test_every_non_public_path_requires_the_token() -> None:
    with TestClient(create_app(make_settings())) as client:
        assert client.get("/mcp").status_code == 401
        assert client.delete("/mcp").status_code == 401
        assert client.get("/anything-else").status_code == 401
        assert client.get("/health/../mcp").status_code == 401


def test_health_stays_public_for_the_container_healthcheck() -> None:
    with TestClient(create_app(make_settings())) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_token_is_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    with TestClient(create_app(make_settings())) as client:
        client.post(
            "/mcp",
            headers={**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}-wrong"},
            json=INITIALIZE,
        )
        client.post(
            "/mcp", headers={**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}"}, json=INITIALIZE
        )

    assert "Rejected unauthenticated request: POST /mcp" in caplog.text
    assert TOKEN not in caplog.text
    assert "t0ken-" not in caplog.text


def test_token_is_hidden_from_settings_repr() -> None:
    settings = make_settings()

    assert TOKEN not in repr(settings)
    assert TOKEN not in str(settings)
    assert TOKEN not in settings.model_dump_json()


def test_token_is_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVER_COPILOT_AUTH_TOKEN", TOKEN)
    with TestClient(create_app()) as client:
        assert client.post("/mcp", headers=MCP_HEADERS, json=INITIALIZE).status_code == 401
        authorized = client.post(
            "/mcp", headers={**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}"}, json=INITIALIZE
        )
    assert authorized.status_code == 200
