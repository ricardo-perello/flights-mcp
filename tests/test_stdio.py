"""The server speaks MCP on stdio, which is how a local host launches it."""

import asyncio
import sys

from mcp import Client, StdioServerParameters


def test_stdio_lists_tools_and_resolves_zurich():
    server = StdioServerParameters(command=sys.executable, args=["-m", "flights_mcp"])

    async def go():
        async with Client(server) as client:
            listed = await client.list_tools()
            names = sorted(tool.name for tool in listed.tools)
            zurich = await client.call_tool("resolve_airports", {"query": "Zurich"})
            barcelona = await client.call_tool("resolve_airports", {"query": "Barcelona"})
            return names, zurich, barcelona

    names, zurich, barcelona = asyncio.run(go())
    assert names == ["resolve_airports", "search_flights"]
    assert zurich.is_error is False
    assert zurich.structured_content["airports"][0]["iata"] == "ZRH"
    assert barcelona.structured_content["airports"][0]["iata"] == "BCN"
