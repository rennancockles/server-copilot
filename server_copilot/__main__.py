import logging
import sys

import uvicorn

from server_copilot.app import create_app
from server_copilot.auth import AuthConfigError
from server_copilot.config import get_settings


def main() -> None:
    settings = get_settings()
    try:
        app = create_app(settings)
    except AuthConfigError as exc:
        sys.exit(f"Configuration error: {exc}")
    if settings.auth_disabled:
        logging.getLogger("uvicorn.error").warning(
            "Authentication is DISABLED: anyone who can reach this port can use every tool."
        )
    # The access log would print the secret in /mcp/<token> request lines.
    uvicorn.run(app, host=settings.host, port=settings.port, access_log=False)


if __name__ == "__main__":
    main()
