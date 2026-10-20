import hmac
import logging
import re

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

MIN_TOKEN_LENGTH = 32
PUBLIC_PATHS = frozenset({"/health"})
MCP_PATH = "/mcp"
URL_TOKEN_PREFIX = "/mcp/"
_URL_SAFE = re.compile(r"[A-Za-z0-9_-]+")


class AuthConfigError(Exception):
    """Authentication is misconfigured; the message tells the operator how to fix it."""


def resolve_token(token: str | None, *, auth_disabled: bool) -> bytes | None:
    """Return the token to enforce, or None when auth is explicitly disabled."""
    if auth_disabled:
        return None
    if not token:
        raise AuthConfigError(
            "SERVER_COPILOT_AUTH_TOKEN is not set. Generate one with "
            "'openssl rand -hex 32' or, for local development only, set "
            "SERVER_COPILOT_AUTH_DISABLED=true."
        )
    if len(token) < MIN_TOKEN_LENGTH:
        raise AuthConfigError(
            f"SERVER_COPILOT_AUTH_TOKEN must be at least {MIN_TOKEN_LENGTH} characters long."
        )
    return token.encode()


def resolve_url_token(
    url_token: str | None, *, bearer_token: str | None, auth_disabled: bool
) -> bytes | None:
    """Return the secret of the /mcp/<token> endpoint, or None when that endpoint is off."""
    if not url_token:
        return None
    if auth_disabled:
        raise AuthConfigError(
            "SERVER_COPILOT_URL_TOKEN needs authentication enabled: unset "
            "SERVER_COPILOT_AUTH_DISABLED."
        )
    if len(url_token) < MIN_TOKEN_LENGTH:
        raise AuthConfigError(
            f"SERVER_COPILOT_URL_TOKEN must be at least {MIN_TOKEN_LENGTH} characters long."
        )
    if not _URL_SAFE.fullmatch(url_token):
        raise AuthConfigError(
            "SERVER_COPILOT_URL_TOKEN becomes a URL path segment: use only letters, digits, "
            "'-' and '_' (openssl rand -hex 32 is fine)."
        )
    if url_token == bearer_token:
        raise AuthConfigError(
            "SERVER_COPILOT_URL_TOKEN must differ from SERVER_COPILOT_AUTH_TOKEN, so the bearer "
            "token never ends up in a URL."
        )
    return url_token.encode()


def _bearer_credentials(scope: Scope) -> bytes | None:
    headers: list[tuple[bytes, bytes]] = scope["headers"]
    for name, value in headers:
        if name == b"authorization":
            scheme, _, credentials = value.partition(b" ")
            return credentials.strip() if scheme.lower() == b"bearer" else None
    return None


def _loggable_path(path: str) -> str:
    return f"{URL_TOKEN_PREFIX}<redacted>" if path.startswith(URL_TOKEN_PREFIX) else path


class AuthMiddleware:
    """Bearer token on /mcp; optionally a secret in the path (/mcp/<token>) for clients that
    cannot send headers (ChatGPT connectors)."""

    def __init__(self, app: ASGIApp, token: bytes, url_token: bytes | None = None) -> None:
        self.app = app
        self._token = token
        self._url_token = url_token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return
        if self._url_token is not None and scope["path"].startswith(URL_TOKEN_PREFIX):
            await self._handle_url_token(scope, receive, send)
            return
        credentials = _bearer_credentials(scope)
        if credentials is not None and hmac.compare_digest(credentials, self._token):
            await self.app(scope, receive, send)
            return
        self._log_rejection(scope)
        response = JSONResponse(
            {"error": "unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
        await response(scope, receive, send)

    async def _handle_url_token(self, scope: Scope, receive: Receive, send: Send) -> None:
        assert self._url_token is not None
        supplied = scope["path"][len(URL_TOKEN_PREFIX) :].encode()
        if not hmac.compare_digest(supplied, self._url_token):
            self._log_rejection(scope)
            await JSONResponse({"error": "not found"}, status_code=404)(scope, receive, send)
            return
        if scope["method"] != "POST":
            # Stateless JSON mode never streams to the client, so only POST is served here.
            response = JSONResponse(
                {"error": "only POST is supported"}, status_code=405, headers={"Allow": "POST"}
            )
            await response(scope, receive, send)
            return
        await self.app({**scope, "path": MCP_PATH, "raw_path": MCP_PATH.encode()}, receive, send)

    @staticmethod
    def _log_rejection(scope: Scope) -> None:
        client = scope.get("client")
        logger.warning(
            "Rejected unauthenticated request: %s %s from %s",
            scope["method"],
            _loggable_path(scope["path"]),
            client[0] if client else "unknown",
        )
