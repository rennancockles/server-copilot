import logging

import pytest
from pydantic import SecretStr
from starlette.testclient import TestClient

from server_copilot.app import create_app
from server_copilot.auth import MIN_TOKEN_LENGTH, AuthConfigError, resolve_url_token
from server_copilot.config import Settings

pytestmark = pytest.mark.unit

BEARER = "b3arer-" + "x" * 40
URL_TOKEN = "url-" + "y" * 40
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}
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
PING = {
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/call",
    "params": {"name": "ping", "arguments": {}},
}


def make_settings() -> Settings:
    return Settings(auth_token=SecretStr(BEARER), url_token=SecretStr(URL_TOKEN))


def test_url_token_serves_the_mcp_endpoint() -> None:
    with TestClient(create_app(make_settings())) as client:
        response = client.post(f"/mcp/{URL_TOKEN}", headers=MCP_HEADERS, json=INITIALIZE)

    assert response.status_code == 200
    assert "server-copilot" in response.text


def test_tool_call_works_in_one_stateless_request() -> None:
    with TestClient(create_app(make_settings())) as client:
        response = client.post(f"/mcp/{URL_TOKEN}", headers=MCP_HEADERS, json=PING)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["result"]["content"][0]["text"] == "ok"


@pytest.mark.parametrize(
    "suffix",
    ["x", "-wrong", "/", "/extra", "%2e", ""],
)
def test_wrong_secret_gets_404(suffix: str) -> None:
    path = f"/mcp/{URL_TOKEN}{suffix}" if suffix else "/mcp/"
    with TestClient(create_app(make_settings())) as client:
        response = client.post(path, headers=MCP_HEADERS, json=INITIALIZE)

    assert response.status_code == 404
    assert response.json() == {"error": "not found"}


def test_a_short_prefix_of_the_secret_is_not_enough() -> None:
    with TestClient(create_app(make_settings())) as client:
        response = client.post(f"/mcp/{URL_TOKEN[:-1]}", headers=MCP_HEADERS, json=INITIALIZE)

    assert response.status_code == 404


@pytest.mark.parametrize("method", ["GET", "DELETE", "PUT"])
def test_only_post_is_served_on_the_secret_url(method: str) -> None:
    with TestClient(create_app(make_settings())) as client:
        response = client.request(method, f"/mcp/{URL_TOKEN}", headers=MCP_HEADERS)

    assert response.status_code == 405
    assert response.headers["allow"] == "POST"


def test_wrong_secret_is_judged_before_the_method() -> None:
    with TestClient(create_app(make_settings())) as client:
        response = client.get("/mcp/not-the-secret", headers=MCP_HEADERS)

    assert response.status_code == 404


def test_bearer_token_still_works_on_mcp() -> None:
    headers = {**MCP_HEADERS, "Authorization": f"Bearer {BEARER}"}
    with TestClient(create_app(make_settings())) as client:
        authorized = client.post("/mcp", headers=headers, json=INITIALIZE)
        anonymous = client.post("/mcp", headers=MCP_HEADERS, json=INITIALIZE)

    assert authorized.status_code == 200
    assert anonymous.status_code == 401


def test_the_secret_is_not_accepted_as_a_bearer_token() -> None:
    headers = {**MCP_HEADERS, "Authorization": f"Bearer {URL_TOKEN}"}
    with TestClient(create_app(make_settings())) as client:
        response = client.post("/mcp", headers=headers, json=INITIALIZE)

    assert response.status_code == 401


def test_endpoint_is_off_without_a_url_token() -> None:
    settings = Settings(auth_token=SecretStr(BEARER))
    with TestClient(create_app(settings)) as client:
        response = client.post(f"/mcp/{URL_TOKEN}", headers=MCP_HEADERS, json=INITIALIZE)

    assert response.status_code == 401


def test_health_stays_public() -> None:
    with TestClient(create_app(make_settings())) as client:
        assert client.get("/health").status_code == 200


def test_secret_never_reaches_the_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    guess = "a-guess-that-should-not-be-logged"
    with TestClient(create_app(make_settings())) as client:
        client.post(f"/mcp/{guess}", headers=MCP_HEADERS, json=INITIALIZE)
        client.post(f"/mcp/{URL_TOKEN}", headers=MCP_HEADERS, json=INITIALIZE)
        client.get(f"/mcp/{URL_TOKEN}x")

    # The test client (httpx2) logs the URLs it requests; only the server's own loggers matter.
    server_logs = "\n".join(
        record.getMessage() for record in caplog.records if not record.name.startswith("httpx")
    )
    assert "Rejected unauthenticated request: POST /mcp/<redacted>" in server_logs
    assert guess not in server_logs
    assert URL_TOKEN not in server_logs


def test_unconfigured_url_token_is_ignored() -> None:
    assert resolve_url_token(None, bearer_token=BEARER, auth_disabled=False) is None
    assert resolve_url_token("", bearer_token=BEARER, auth_disabled=False) is None


def test_valid_url_token_is_returned_as_bytes() -> None:
    assert resolve_url_token(URL_TOKEN, bearer_token=BEARER, auth_disabled=False) == (
        URL_TOKEN.encode()
    )


def test_short_url_token_is_rejected() -> None:
    with pytest.raises(AuthConfigError, match=str(MIN_TOKEN_LENGTH)):
        resolve_url_token("short", bearer_token=BEARER, auth_disabled=False)


@pytest.mark.parametrize(
    "token", ["a/b" + "c" * 40, "a?b" + "c" * 40, "a%2fb" + "c" * 40, "a b" + "c" * 40]
)
def test_url_token_must_be_a_safe_path_segment(token: str) -> None:
    with pytest.raises(AuthConfigError, match="letters, digits"):
        resolve_url_token(token, bearer_token=BEARER, auth_disabled=False)


def test_url_token_must_differ_from_the_bearer_token() -> None:
    with pytest.raises(AuthConfigError, match="must differ"):
        resolve_url_token(BEARER, bearer_token=BEARER, auth_disabled=False)


def test_url_token_requires_authentication_to_be_enabled() -> None:
    with pytest.raises(AuthConfigError, match="AUTH_DISABLED"):
        create_app(Settings(auth_disabled=True, url_token=SecretStr(URL_TOKEN)))


def test_url_token_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVER_COPILOT_AUTH_TOKEN", BEARER)
    monkeypatch.setenv("SERVER_COPILOT_URL_TOKEN", URL_TOKEN)
    with TestClient(create_app()) as client:
        response = client.post(f"/mcp/{URL_TOKEN}", headers=MCP_HEADERS, json=PING)

    assert response.status_code == 200


def test_url_token_is_hidden_from_settings_repr() -> None:
    settings = make_settings()

    assert URL_TOKEN not in repr(settings)
    assert URL_TOKEN not in settings.model_dump_json()
