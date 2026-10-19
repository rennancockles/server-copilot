from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from server_copilot.config import get_settings
from server_copilot.containers import register_container_tools
from server_copilot.docker import DockerClient


def create_server(docker: DockerClient | None = None) -> MCPServer:
    client = docker or DockerClient(get_settings().docker_host)

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncGenerator[None]:
        try:
            yield
        finally:
            await client.aclose()

    mcp = MCPServer("server-copilot", lifespan=lifespan)

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    @mcp.tool()
    def ping() -> str:
        """Check that the server-copilot MCP server is alive."""
        return "ok"

    register_container_tools(mcp, client)

    return mcp
