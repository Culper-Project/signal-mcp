"""Shared test helpers."""
from mcp.types import CallToolRequestParams
from signal_mcp.server import call_tool as _call_tool, get_client, TOOLS  # re-export for tests


async def call_tool(name: str, arguments: dict):
    """Thin shim so existing tests keep their (name, args) call signature."""
    return (await _call_tool(CallToolRequestParams(name=name, arguments=arguments))).content
