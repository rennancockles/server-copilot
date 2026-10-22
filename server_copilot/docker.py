import re
from collections.abc import Generator
from contextlib import contextmanager
from http import HTTPStatus
from types import TracebackType
from typing import Any, Self

import httpx2

DEFAULT_TIMEOUT = 10.0

# Tecnativa/docker-socket-proxy env var that gates each Docker API section.
PROXY_PERMISSIONS = {
    "containers": "CONTAINERS",
    "images": "IMAGES",
    "info": "INFO",
    "networks": "NETWORKS",
    "volumes": "VOLUMES",
    "system": "SYSTEM",
    "events": "EVENTS",
    "version": "VERSION",
    "_ping": "PING",
}

_API_VERSION_PREFIX = re.compile(r"^/v\d+(\.\d+)?(?=/)")


class DockerError(Exception):
    """Docker request failed; the message is safe to show to the MCP client."""


class DockerUnavailableError(DockerError):
    """Docker (or the docker-socket-proxy in front of it) could not be reached."""


class DockerPermissionError(DockerError):
    """The docker-socket-proxy blocked the endpoint."""


class DockerNotFoundError(DockerError):
    """The requested Docker object does not exist."""


def base_url(docker_host: str) -> str:
    scheme, sep, rest = docker_host.partition("://")
    if not sep or not rest:
        raise ValueError(f"Invalid Docker host {docker_host!r}; expected tcp://host:port")
    if scheme == "tcp":
        return f"http://{rest.rstrip('/')}"
    if scheme in ("http", "https"):
        return docker_host.rstrip("/")
    raise ValueError(
        f"Unsupported Docker host scheme {scheme!r}; "
        "use tcp://host:port pointing at the docker-socket-proxy"
    )


def required_permission(path: str) -> str | None:
    section = _API_VERSION_PREFIX.sub("", path).lstrip("/").split("/", 1)[0]
    return PROXY_PERMISSIONS.get(section)


def _error_detail(response: httpx2.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip()
    message = payload.get("message") if isinstance(payload, dict) else None
    return message if isinstance(message, str) else response.text.strip()


class DockerClient:
    def __init__(
        self,
        docker_host: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url(docker_host)
        self._client = httpx2.AsyncClient(
            base_url=self.base_url, timeout=timeout, transport=transport, trust_env=False
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_json(
        self, path: str, params: dict[str, Any] | None = None, *, time_limit: float | None = None
    ) -> Any:
        return (await self._get(path, params, time_limit)).json()

    @contextmanager
    def _transport_errors(self, path: str) -> Generator[None]:
        try:
            yield
        except httpx2.TimeoutException as exc:
            raise DockerUnavailableError(
                f"Timed out talking to Docker at {self.base_url} (GET {path})."
            ) from exc
        except httpx2.TransportError as exc:
            raise DockerUnavailableError(
                f"Cannot connect to Docker at {self.base_url}: {exc}. "
                "Check that docker-socket-proxy is running and reachable on docker-api."
            ) from exc

    async def _get(
        self, path: str, params: dict[str, Any] | None, time_limit: float | None = None
    ) -> httpx2.Response:
        with self._transport_errors(path):
            response = await self._client.get(
                path,
                params=params,
                timeout=httpx2.USE_CLIENT_DEFAULT if time_limit is None else time_limit,
            )
        if response.is_success:
            return response
        raise self._error_for(response, path)

    async def get_bytes(
        self, path: str, params: dict[str, Any] | None = None, *, max_bytes: int
    ) -> tuple[bytes, bool]:
        """Read a response body, stopping after max_bytes. Returns (data, was_cut_short)."""
        with self._transport_errors(path):
            async with self._client.stream("GET", path, params=params) as response:
                if not response.is_success:
                    await response.aread()
                    raise self._error_for(response, path)
                buffer = bytearray()
                async for chunk in response.aiter_bytes():
                    buffer += chunk
                    if len(buffer) > max_bytes:
                        return bytes(buffer[:max_bytes]), True
        return bytes(buffer), False

    def _error_for(self, response: httpx2.Response, path: str) -> DockerError:
        status = response.status_code
        if status == HTTPStatus.FORBIDDEN:
            permission = required_permission(path)
            hint = (
                f"Enable {permission}=1 on the docker-socket-proxy."
                if permission
                else "This endpoint is not allowed by the docker-socket-proxy configuration."
            )
            return DockerPermissionError(f"docker-socket-proxy denied GET {path} (403). {hint}")
        detail = _error_detail(response)
        if status == HTTPStatus.NOT_FOUND:
            return DockerNotFoundError(f"Docker returned 404 for GET {path}: {detail}")
        return DockerError(f"Docker returned {status} for GET {path}: {detail}")

    async def version(self) -> dict[str, Any]:
        result: dict[str, Any] = await self.get_json("/version")
        return result

    async def check_connection(self) -> dict[str, str]:
        version = await self.version()
        return {
            "docker_version": str(version.get("Version", "unknown")),
            "api_version": str(version.get("ApiVersion", "unknown")),
        }
