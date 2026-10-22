import hmac
import logging

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from server_copilot import oauth
from server_copilot.config import Settings

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


def resolve_oauth(settings: Settings) -> Settings | None:
    """Return the settings when OAuth sign-in is configured, None when it is off."""
    if not settings.oauth_requested:
        return None
    if settings.auth_disabled:
        raise AuthConfigError(
            "OAuth needs authentication enabled: unset SERVER_COPILOT_AUTH_DISABLED."
        )
    if not (settings.authkit_domain and settings.public_url):
        raise AuthConfigError(
            "OAuth needs both SERVER_COPILOT_AUTHKIT_DOMAIN and SERVER_COPILOT_PUBLIC_URL; "
            "set both or neither."
        )
    for name, value in (
        ("SERVER_COPILOT_AUTHKIT_DOMAIN", settings.authkit_domain),
        ("SERVER_COPILOT_PUBLIC_URL", settings.public_url),
    ):
        if not value.startswith("https://"):
            raise AuthConfigError(f"{name} must be an https:// URL.")
    if not settings.oauth_allowed_emails.strip():
        logger.warning(
            "SERVER_COPILOT_OAUTH_ALLOWED_EMAILS is empty: nobody can sign in with OAuth."
        )
    return settings


def _bearer_credentials(scope: Scope) -> bytes | None:
    headers: list[tuple[bytes, bytes]] = scope["headers"]
    for name, value in headers:
        if name == b"authorization":
            scheme, _, credentials = value.partition(b" ")
            return credentials.strip() if scheme.lower() == b"bearer" else None
    return None


class AuthMiddleware:
    """Static bearer token on /mcp, plus OAuth access tokens when sign-in is configured."""

    def __init__(self, app: ASGIApp, token: bytes, oauth_settings: Settings | None = None) -> None:
        self.app = app
        self._token = token
        self._oauth = oauth_settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return
        if self._oauth is not None and scope["path"] == oauth.METADATA_PATH:
            await JSONResponse(oauth.resource_metadata(self._oauth))(scope, receive, send)
            return
        credentials = _bearer_credentials(scope)
        if credentials is not None and hmac.compare_digest(credentials, self._token):
            await self.app(scope, receive, send)
            return
        if self._oauth is None or not credentials:
            self._log_rejection(scope)
            await self._unauthorized()(scope, receive, send)
            return
        await self._handle_oauth(scope, receive, send, credentials)

    async def _handle_oauth(
        self, scope: Scope, receive: Receive, send: Send, credentials: bytes
    ) -> None:
        assert self._oauth is not None
        try:
            await oauth.authorize(self._oauth, credentials.decode("utf-8", errors="replace"))
        except oauth.OAuthUnauthorizedError as exc:
            self._log_rejection(scope)
            await self._unauthorized(invalid_token=exc.description)(scope, receive, send)
        except oauth.OAuthForbiddenError:
            self._log_rejection(scope)
            await JSONResponse({"error": "forbidden"}, status_code=403)(scope, receive, send)
        else:
            await self.app(scope, receive, send)

    def _unauthorized(self, invalid_token: str | None = None) -> JSONResponse:
        challenge = "Bearer"
        if self._oauth is not None:
            parts = []
            if invalid_token is not None:
                parts += ['error="invalid_token"', f'error_description="{invalid_token}"']
            parts.append(f'resource_metadata="{oauth.metadata_url(self._oauth)}"')
            challenge = f"Bearer {', '.join(parts)}"
        return JSONResponse(
            {"error": "invalid_token" if invalid_token is not None else "unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": challenge},
        )

    @staticmethod
    def _log_rejection(scope: Scope) -> None:
        client = scope.get("client")
        logger.warning(
            "Rejected unauthenticated request: %s %s from %s",
            scope["method"],
            scope["path"],
            client[0] if client else "unknown",
        )
