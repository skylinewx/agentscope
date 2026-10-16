# -*- coding: utf-8 -*-
"""Local MCP transport fixture. No network or browser is needed."""

from mcp.server.fastmcp import FastMCP

server = FastMCP("resource-lifecycle-test")


@server.tool()
async def echo(text: str) -> str:
    """Echo the supplied text."""
    return text


if __name__ == "__main__":
    server.run(transport="stdio")
