from server_copilot.config import get_settings
from server_copilot.server import create_server


def main() -> None:
    settings = get_settings()
    create_server().run("streamable-http", host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
