from starlette.applications import Starlette

from server_copilot.auth import BearerAuthMiddleware, resolve_token
from server_copilot.config import Settings, get_settings
from server_copilot.server import create_server


def create_app(settings: Settings | None = None) -> Starlette:
    settings = settings or get_settings()
    token = resolve_token(
        settings.auth_token.get_secret_value() if settings.auth_token else None,
        auth_disabled=settings.auth_disabled,
    )
    app = create_server().streamable_http_app(host=settings.host)
    if token is not None:
        app.add_middleware(BearerAuthMiddleware, token=token)
    return app
