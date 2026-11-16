"""MCP 协议层端到端验证：以 DSH 将采用的方式（stdio 子进程）连接并调用。"""
import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PARAMS = StdioServerParameters(
    command=sys.executable,
    args=["mcp_server.py"],
    cwd=str(Path(__file__).resolve().parents[1]),
)


async def main():
    async with stdio_client(PARAMS) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            name = init.serverInfo.name
            print(f"握手 PASS: server={name}")

            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            assert set(names) >= {"metasearch", "citations", "oa_lookup", "list_sources"}, names
            print(f"list_tools PASS: {names}")

            res = await session.call_tool("metasearch", {"q": "soil moisture sensor", "limit": 3})
            data = res.content[0].text
            d = json.loads(data)
            assert d["total"] > 0 and len(d["results"]) == d["total"]
            print(f"metasearch 调用 PASS: total={d['total']}, 首条={d['results'][0]['title'][:36]}")

            res = await session.call_tool("citations", {"id": "10.13031/2013.21723", "limit": 3})
            d = json.loads(res.content[0].text)
            assert d["work_id"] == "W2039635063"
            print(f"citations 调用 PASS: citing={len(d['citing'])}")

            res = await session.call_tool("list_sources", {})
            d = json.loads(res.content[0].text)
            assert len(d["sources"]) == 10
            print("list_sources 调用 PASS: 10 源")

    print("\nMCP 协议层端到端全 PASS")


asyncio.run(main())
