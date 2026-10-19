import hmac
import logging

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

MIN_TOKEN_LENGTH = 32
PUBLIC_PATHS = frozenset({"/health"})


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


def _bearer_credentials(scope: Scope) -> bytes | None:
    headers: list[tuple[bytes, bytes]] = scope["headers"]
    for name, value in headers:
        if name == b"authorization":
            scheme, _, credentials = value.partition(b" ")
            return credentials.strip() if scheme.lower() == b"bearer" else None
    return None


class BearerAuthMiddleware:
    def __init__(self, app: ASGIApp, token: bytes) -> None:
        self.app = app
        self._token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return
        credentials = _bearer_credentials(scope)
        if credentials is not None and hmac.compare_digest(credentials, self._token):
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        logger.warning(
            "Rejected unauthenticated request: %s %s from %s",
            scope["method"],
            scope["path"],
            client[0] if client else "unknown",
        )
        response = JSONResponse(
            {"error": "unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
        await response(scope, receive, send)
