from mcp.server.mcpserver import MCPServer


def create_server() -> MCPServer:
    mcp = MCPServer("server-copilot")

    @mcp.tool()
    def ping() -> str:
        """Check that the server-copilot MCP server is alive."""
        return "ok"

    return mcp
