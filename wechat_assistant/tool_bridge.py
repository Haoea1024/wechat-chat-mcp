"""Use the same MCP tools from an agent shell while native discovery is unavailable."""
import asyncio
from datetime import timedelta

from .setup_client import server_spec


async def invoke(root, name=None, arguments=None):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    async with stdio_client(StdioServerParameters(**server_spec(root))) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=900)) as session:
            initialized = await session.initialize()
            if name is None:
                listing = await session.list_tools()
                return {'instructions': initialized.instructions,
                        'tools': [tool.model_dump(mode='json', exclude_none=True) for tool in listing.tools]}
            result = await session.call_tool(name, arguments or {})
            # Structured content already represents the text result; don't duplicate it.
            payload = result.structuredContent
            if payload is None:
                payload = [item.model_dump(mode='json', exclude_none=True) for item in result.content]
            return {'isError': bool(result.isError), 'result': payload}


def call(root, name=None, arguments=None):
    return asyncio.run(asyncio.wait_for(invoke(root, name, arguments), timeout=960))
