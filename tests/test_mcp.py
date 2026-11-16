"""MCP stdio 生命周期与工具参数边界的离线测试。"""
import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_mcp_stdio_lifecycle_and_tool_list():
    async def run():
        params = StdioServerParameters(command=sys.executable, args=["mcp_server.py"])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert {t.name for t in tools.tools} == {
                    "metasearch", "citations", "oa_lookup", "list_sources"
                }
                response = await session.call_tool(
                    "oa_lookup", {"doi": "https://example.com/not-a-doi"}
                )
                payload = json.loads(response.content[0].text)
                assert "格式不正确" in payload["error"]
    asyncio.run(run())
