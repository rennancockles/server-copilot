from starlette.applications import Starlette

from server_copilot.auth import AuthMiddleware, resolve_token, resolve_url_token
from server_copilot.config import Settings, get_settings
from server_copilot.server import create_server


def create_app(settings: Settings | None = None) -> Starlette:
    settings = settings or get_settings()
    bearer = settings.auth_token.get_secret_value() if settings.auth_token else None
    token = resolve_token(bearer, auth_disabled=settings.auth_disabled)
    url_token = resolve_url_token(
        settings.url_token.get_secret_value() if settings.url_token else None,
        bearer_token=bearer,
        auth_disabled=settings.auth_disabled,
    )
    # Tools keep no state between calls, so stateless JSON responses are enough and suit clients
    # that cannot hold a session or an event stream.
    app = create_server().streamable_http_app(
        host=settings.host, stateless_http=True, json_response=True
    )
    if token is not None:
        app.add_middleware(AuthMiddleware, token=token, url_token=url_token)
    return app
