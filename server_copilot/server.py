from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from server_copilot.config import get_settings
from server_copilot.containers import register_container_tools
from server_copilot.diagnostics import register_diagnostic_tools
from server_copilot.docker import DockerClient
from server_copilot.host import parse_disks, register_host_tools
from server_copilot.logs import register_log_tools
from server_copilot.stats import register_stats_tools


def create_server(docker: DockerClient | None = None) -> MCPServer:
    settings = get_settings()
    client = docker or DockerClient(settings.docker_host)

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncGenerator[None]:
        try:
            yield
        finally:
            await client.aclose()

    mcp = MCPServer("server-copilot", lifespan=lifespan)

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    @mcp.tool()
    def ping() -> str:
        """Check that the server-copilot MCP server is alive."""
        return "ok"

    register_container_tools(mcp, client)
    register_log_tools(mcp, client)
    register_stats_tools(mcp, client)
    register_host_tools(mcp, parse_disks(settings.host_disks))
    register_diagnostic_tools(mcp, client)

    return mcp
