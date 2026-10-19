from collections.abc import Callable

import httpx2
import pytest

from server_copilot.docker import (
    DockerClient,
    DockerError,
    DockerNotFoundError,
    DockerPermissionError,
    DockerUnavailableError,
    base_url,
    required_permission,
)

pytestmark = pytest.mark.unit

Handler = Callable[[httpx2.Request], httpx2.Response]


def make_client(handler: Handler) -> DockerClient:
    return DockerClient("tcp://proxy:2375", transport=httpx2.MockTransport(handler))


@pytest.mark.parametrize(
    ("docker_host", "expected"),
    [
        ("tcp://docker-socket-proxy:2375", "http://docker-socket-proxy:2375"),
        ("tcp://proxy:2375/", "http://proxy:2375"),
        ("http://proxy:2375", "http://proxy:2375"),
        ("https://proxy.example/", "https://proxy.example"),
    ],
)
def test_base_url(docker_host: str, expected: str) -> None:
    assert base_url(docker_host) == expected


@pytest.mark.parametrize("docker_host", ["", "proxy:2375", "tcp://", "unix:///var/run/docker.sock"])
def test_base_url_rejects_invalid_hosts(docker_host: str) -> None:
    with pytest.raises(ValueError, match="Docker host"):
        base_url(docker_host)


@pytest.mark.parametrize(
    ("path", "permission"),
    [
        ("/containers/json", "CONTAINERS"),
        ("/v1.43/containers/abc/logs", "CONTAINERS"),
        ("/info", "INFO"),
        ("/system/df", "SYSTEM"),
        ("/events", "EVENTS"),
        ("/v1.43/version", "VERSION"),
        ("/unknown/thing", None),
    ],
)
def test_required_permission(path: str, permission: str | None) -> None:
    assert required_permission(path) == permission


async def test_check_connection_returns_versions() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/version"
        return httpx2.Response(200, json={"Version": "27.3.1", "ApiVersion": "1.47"})

    async with make_client(handler) as client:
        assert await client.check_connection() == {
            "docker_version": "27.3.1",
            "api_version": "1.47",
        }


async def test_get_json_passes_query_params() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.params["all"] == "true"
        return httpx2.Response(200, json=[{"Id": "abc"}])

    async with make_client(handler) as client:
        assert await client.get_json("/containers/json", {"all": "true"}) == [{"Id": "abc"}]


async def test_403_explains_missing_proxy_permission() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(403, text="Forbidden")

    async with make_client(handler) as client:
        with pytest.raises(DockerPermissionError) as exc_info:
            await client.get_json("/containers/json")
    message = str(exc_info.value)
    assert "/containers/json" in message
    assert "CONTAINERS=1" in message


async def test_403_on_unmapped_endpoint_has_generic_hint() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(403)

    async with make_client(handler) as client:
        with pytest.raises(DockerPermissionError, match="not allowed by the docker-socket-proxy"):
            await client.get_json("/plugins")


async def test_404_raises_not_found_with_docker_message() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(404, json={"message": "No such container: abc"})

    async with make_client(handler) as client:
        with pytest.raises(DockerNotFoundError, match="No such container: abc"):
            await client.get_json("/containers/abc/json")


async def test_other_http_errors_raise_docker_error() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(500, text="boom")

    async with make_client(handler) as client:
        with pytest.raises(DockerError, match="500.*boom"):
            await client.get_json("/info")


async def test_connection_failure_is_unavailable() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("name resolution failed")

    async with make_client(handler) as client:
        with pytest.raises(DockerUnavailableError, match="Cannot connect to Docker at"):
            await client.check_connection()


async def test_timeout_is_unavailable() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow")

    async with make_client(handler) as client:
        with pytest.raises(DockerUnavailableError, match="Timed out"):
            await client.get_json("/info")
