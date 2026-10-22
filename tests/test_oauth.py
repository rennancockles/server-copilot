import logging
import time
from typing import Any

import httpx2 as httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr
from starlette.testclient import TestClient

from server_copilot import oauth
from server_copilot.app import create_app
from server_copilot.auth import AuthConfigError
from server_copilot.config import Settings

pytestmark = pytest.mark.unit

ISSUER = "https://copilot-test.authkit.app"
PUBLIC_URL = "https://server-copilot.example.com"
RESOURCE = f"{PUBLIC_URL}/mcp"
METADATA_URL = f"{PUBLIC_URL}/.well-known/oauth-protected-resource/mcp"
KANBAI_RESOURCE = "https://kanbai.example.com/mcp/board-token"
STATIC_TOKEN = "t0ken-" + "x" * 40
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
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


def make_settings(**values: Any) -> Settings:
    defaults: dict[str, Any] = {
        "auth_token": SecretStr(STATIC_TOKEN),
        "authkit_domain": ISSUER + "/",
        "public_url": PUBLIC_URL + "/",
        "oauth_allowed_emails": "me@example.com",
    }
    return Settings(_env_file=None, **{**defaults, **values})  # type: ignore[arg-type]


def make_bearer(**claims: Any) -> str:
    payload = {
        "iss": ISSUER,
        "sub": "user_01",
        "aud": RESOURCE,
        "exp": int(time.time()) + 300,
        "email": "me@example.com",
        "email_verified": True,
        **claims,
    }
    return jwt.encode(payload, KEY, algorithm="RS256")


def call(settings: Settings, bearer: str | None = None) -> httpx.Response:
    headers = dict(MCP_HEADERS)
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    with TestClient(create_app(settings)) as client:
        return client.post("/mcp", headers=headers, json=INITIALIZE)


@pytest.fixture(autouse=True)
def _signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    async def signing_key(_issuer: str, _token: str) -> Any:
        return KEY.public_key()

    monkeypatch.setattr(oauth, "signing_key", signing_key)


def test_a_valid_token_of_an_allowed_email_is_accepted() -> None:
    response = call(make_settings(), make_bearer())

    assert response.status_code == 200
    assert "server-copilot" in response.text


def test_the_email_is_matched_case_insensitively() -> None:
    settings = make_settings(oauth_allowed_emails=" Me@Example.com , other@example.com ")

    assert call(settings, make_bearer(email="ME@example.COM")).status_code == 200


def test_an_anonymous_request_is_challenged_with_the_metadata_url() -> None:
    response = call(make_settings())

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == f'Bearer resource_metadata="{METADATA_URL}"'


def test_the_static_bearer_token_keeps_working() -> None:
    assert call(make_settings(), STATIC_TOKEN).status_code == 200


def test_without_oauth_the_challenge_stays_a_plain_bearer() -> None:
    settings = make_settings(authkit_domain=None, public_url=None)

    response = call(settings)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert call(settings, make_bearer()).status_code == 401


def test_an_expired_token_is_refused() -> None:
    response = call(make_settings(), make_bearer(exp=int(time.time()) - 10))

    assert response.status_code == 401
    assert 'error="invalid_token"' in response.headers["www-authenticate"]
    assert f'resource_metadata="{METADATA_URL}"' in response.headers["www-authenticate"]


@pytest.mark.parametrize(
    "audience",
    [
        KANBAI_RESOURCE,
        "https://kanbai.example.com/mcp",
        f"{PUBLIC_URL}/mcp/other",
        PUBLIC_URL,
        "client_01ABC",
        [KANBAI_RESOURCE, "client_01ABC"],
    ],
)
def test_a_token_issued_for_another_mcp_is_refused(audience: str | list[str]) -> None:
    assert call(make_settings(), make_bearer(aud=audience)).status_code == 401


def test_a_token_from_another_issuer_is_refused() -> None:
    assert call(make_settings(), make_bearer(iss="https://evil.authkit.app")).status_code == 401


def test_a_token_signed_by_someone_else_is_refused() -> None:
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode(
        {"iss": ISSUER, "sub": "u", "aud": RESOURCE, "exp": int(time.time()) + 300},
        other,
        algorithm="RS256",
    )

    assert call(make_settings(), forged).status_code == 401


def test_an_unsigned_token_is_refused() -> None:
    payload = {"iss": ISSUER, "sub": "u", "aud": RESOURCE, "exp": int(time.time()) + 300}
    unsigned = jwt.encode(payload, "", algorithm="none")

    assert call(make_settings(), unsigned).status_code == 401


def test_a_token_without_the_required_claims_is_refused() -> None:
    bearer = jwt.encode({"iss": ISSUER, "aud": RESOURCE}, KEY, algorithm="RS256")

    assert call(make_settings(), bearer).status_code == 401


def test_garbage_in_the_bearer_header_is_refused() -> None:
    assert call(make_settings(), "not-a-jwt").status_code == 401


def test_a_valid_token_of_an_email_off_the_allowlist_is_forbidden() -> None:
    response = call(make_settings(), make_bearer(email="stranger@example.com"))

    assert response.status_code == 403
    assert response.json() == {"error": "forbidden"}


def test_an_unverified_email_is_forbidden() -> None:
    assert call(make_settings(), make_bearer(email_verified=False)).status_code == 403


def test_an_empty_allowlist_lets_nobody_in() -> None:
    assert call(make_settings(oauth_allowed_emails=""), make_bearer()).status_code == 403


def test_the_email_is_read_from_userinfo_when_the_token_has_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, str] = {}

    async def fetch_userinfo(authkit_issuer: str, token: str) -> dict[str, Any]:
        seen["issuer"], seen["token"] = authkit_issuer, token
        return {"email": "me@example.com", "email_verified": True}

    monkeypatch.setattr(oauth, "fetch_userinfo", fetch_userinfo)
    claims = {"iss": ISSUER, "sub": "u", "aud": RESOURCE, "exp": int(time.time()) + 300}
    bearer = jwt.encode(claims, KEY, algorithm="RS256")

    assert call(make_settings(), bearer).status_code == 200
    assert seen == {"issuer": ISSUER, "token": bearer}


def test_a_failing_userinfo_means_the_identity_could_not_be_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fetch_userinfo(_issuer: str, _token: str) -> dict[str, Any]:
        raise httpx.ConnectError("down")

    monkeypatch.setattr(oauth, "fetch_userinfo", fetch_userinfo)
    claims = {"iss": ISSUER, "sub": "u", "aud": RESOURCE, "exp": int(time.time()) + 300}
    bearer = jwt.encode(claims, KEY, algorithm="RS256")

    assert call(make_settings(), bearer).status_code == 401


def test_only_mcp_and_health_and_the_metadata_are_reachable() -> None:
    with TestClient(create_app(make_settings())) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/anything-else").status_code == 401
        assert client.get("/.well-known/oauth-protected-resource").status_code == 401
        assert client.get("/mcp").status_code == 401


def test_the_metadata_names_the_mcp_url_and_a_normalised_issuer() -> None:
    with TestClient(create_app(make_settings())) as client:
        response = client.get("/.well-known/oauth-protected-resource/mcp")

    assert response.status_code == 200
    assert response.json() == {
        "resource": RESOURCE,
        "authorization_servers": [ISSUER],
        "bearer_methods_supported": ["header"],
    }


def test_the_metadata_is_not_served_when_oauth_is_off() -> None:
    settings = make_settings(authkit_domain=None, public_url=None)

    with TestClient(create_app(settings)) as client:
        assert client.get("/.well-known/oauth-protected-resource/mcp").status_code == 401


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"public_url": None}, "set both or neither"),
        ({"authkit_domain": None}, "set both or neither"),
        ({"authkit_domain": "http://copilot-test.authkit.app"}, "SERVER_COPILOT_AUTHKIT_DOMAIN"),
        ({"public_url": "http://server-copilot.example.com"}, "SERVER_COPILOT_PUBLIC_URL"),
        ({"auth_disabled": True}, "unset SERVER_COPILOT_AUTH_DISABLED"),
    ],
)
def test_a_partial_or_insecure_oauth_configuration_refuses_to_start(
    values: dict[str, Any], message: str
) -> None:
    with pytest.raises(AuthConfigError, match=message):
        create_app(make_settings(**values))


def test_an_empty_allowlist_is_warned_about_at_startup(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)

    create_app(make_settings(oauth_allowed_emails=""))

    assert "SERVER_COPILOT_OAUTH_ALLOWED_EMAILS is empty" in caplog.text


def test_the_access_token_is_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    bearer = make_bearer(aud=KANBAI_RESOURCE)

    call(make_settings(), bearer)

    assert bearer not in caplog.text


def test_the_log_says_where_the_email_came_from_without_revealing_it(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def fetch_userinfo(_issuer: str, _token: str) -> dict[str, Any]:
        return {"email": "me@example.com", "email_verified": True}

    monkeypatch.setattr(oauth, "fetch_userinfo", fetch_userinfo)
    caplog.set_level(logging.INFO, logger=oauth.logger.name)
    claims = {"iss": ISSUER, "sub": "u", "aud": RESOURCE, "exp": int(time.time()) + 300}
    no_email = jwt.encode(claims, KEY, algorithm="RS256")
    in_token = make_bearer()

    call(make_settings(), in_token)
    call(make_settings(), no_email)

    messages = [r.getMessage() for r in caplog.records if r.name == oauth.logger.name]
    assert messages == [
        "MCP OAuth: access granted, email read from the token",
        "MCP OAuth: access granted, email read from the userinfo",
    ]
    assert "me@example.com" not in caplog.text
    assert in_token not in caplog.text
