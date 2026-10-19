from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


def create_server() -> MCPServer:
    mcp = MCPServer("server-copilot")

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    @mcp.tool()
    def ping() -> str:
        """Check that the server-copilot MCP server is alive."""
        return "ok"

    return mcp
