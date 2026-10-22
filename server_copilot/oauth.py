"""OAuth for the MCP endpoint, with an external identity provider as the authorization server.

The IdP (WorkOS AuthKit) owns sign-in, consent, PKCE, client registration and token issuing.
This module is only the resource-server half: it validates the access token a client presents on
``/mcp`` and decides whether that person may use the server.

The MCP URL is the OAuth *resource* (RFC 8707): the IdP stamps it into the token's ``aud``, so a
token issued for another MCP (or for the IdP's own client id) is refused here. Who may sign in is a
deny-by-default allowlist of verified emails (``SERVER_COPILOT_OAUTH_ALLOWED_EMAILS``).
"""

import asyncio
import functools
import logging
from typing import Any

import httpx2 as httpx
import jwt

from server_copilot.config import Settings

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 5.0
MCP_PATH = "/mcp"
METADATA_PATH = f"/.well-known/oauth-protected-resource{MCP_PATH}"

#: AuthKit signs its access tokens with RS256 (its OIDC configuration lists nothing else).
_ALGORITHMS = ["RS256"]


class OAuthUnauthorizedError(Exception):
    """The request carries no usable access token (HTTP 401)."""

    def __init__(self, description: str | None = None) -> None:
        self.description = description
        super().__init__(description or "Authentication required.")


class OAuthForbiddenError(Exception):
    """A valid token whose owner may not use the server (HTTP 403)."""


def issuer(settings: Settings) -> str:
    return (settings.authkit_domain or "").rstrip("/")


def _public_url(settings: Settings) -> str:
    return (settings.public_url or "").rstrip("/")


def resource_url(settings: Settings) -> str:
    """The MCP URL: what the IdP must name as the token's audience."""
    return f"{_public_url(settings)}{MCP_PATH}"


def metadata_url(settings: Settings) -> str:
    """Where clients find the protected-resource metadata (RFC 9728, path-suffixed)."""
    return f"{_public_url(settings)}{METADATA_PATH}"


def resource_metadata(settings: Settings) -> dict[str, Any]:
    return {
        "resource": resource_url(settings),
        "authorization_servers": [issuer(settings)],
        "bearer_methods_supported": ["header"],
    }


@functools.lru_cache(maxsize=4)
def _jwks_client(authkit_issuer: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(f"{authkit_issuer}/oauth2/jwks", timeout=HTTP_TIMEOUT)


async def signing_key(authkit_issuer: str, token: str) -> Any:
    """The IdP's public key that signed ``token`` (keys are cached by the JWKS client)."""
    client = _jwks_client(authkit_issuer)
    return (await asyncio.to_thread(client.get_signing_key_from_jwt, token)).key


async def fetch_userinfo(authkit_issuer: str, token: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        response = await client.get(
            f"{authkit_issuer}/oauth2/userinfo", headers={"Authorization": f"Bearer {token}"}
        )
    response.raise_for_status()
    return response.json()  # type: ignore[no-any-return]


async def authorize(settings: Settings, bearer: str) -> str:
    """Check the access token ``bearer`` presented on the MCP endpoint.

    Returns the caller's email. Raises :class:`OAuthUnauthorizedError` for a token that is
    missing, malformed, expired, signed by someone else or issued for another resource, and
    :class:`OAuthForbiddenError` for a valid token whose email is unverified or not allowed.
    """
    authkit_issuer = issuer(settings)
    try:
        key = await signing_key(authkit_issuer, bearer)
        claims = jwt.decode(
            bearer,
            key,
            algorithms=_ALGORITHMS,
            issuer=authkit_issuer,
            audience=resource_url(settings),
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as exc:
        logger.info("MCP OAuth: token refused: %s", exc)
        raise OAuthUnauthorizedError("The access token is invalid or expired.") from exc

    identity = claims
    if "email" not in claims:
        try:
            identity = await fetch_userinfo(authkit_issuer, bearer)
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("MCP OAuth: could not read the user's email: %s", exc)
            raise OAuthUnauthorizedError("The caller's identity could not be confirmed.") from exc

    email = str(identity.get("email", ""))
    if identity.get("email_verified") is not True or not settings.is_oauth_email_allowed(email):
        logger.warning("MCP OAuth: %r is not allowed to use the server", email)
        raise OAuthForbiddenError
    return email.lower()
