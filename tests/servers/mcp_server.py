"""A small stdio MCP server for tests; works with the MCP SDK 1.x (FastMCP) and 2.x (MCPServer)."""

import base64

try:
    from mcp.server.mcpserver import Context, Image, MCPServer as Server
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # MCP SDK 1.x
    from mcp.server.fastmcp import Context, FastMCP as Server, Image
    from mcp.server.fastmcp.exceptions import ToolError

server = Server("pi-python-test")


@server.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@server.tool()
def fail(reason: str) -> str:
    """Always fails; ToolError reports its message to the client."""
    raise ToolError(reason)


@server.tool()
async def count(steps: int, ctx: Context) -> str:
    """Count up, reporting progress."""
    for step in range(1, steps + 1):
        await ctx.report_progress(step, steps, f"step {step}")
    return f"counted {steps}"


@server.tool(name="pixel.png")
def pixel() -> Image:
    """Return a 1x1 PNG."""
    data = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    return Image(data=data, format="png")


if __name__ == "__main__":
    import sys

    if sys.argv[1:2] == ["--http"]:  # streamable HTTP on 127.0.0.1:<port>/mcp
        port = int(sys.argv[2])
        try:
            server.run(transport="streamable-http", host="127.0.0.1", port=port)
        except TypeError:  # MCP SDK 1.x takes the address from the server's settings
            server.settings.host, server.settings.port = "127.0.0.1", port
            server.run(transport="streamable-http")
    else:
        server.run()
