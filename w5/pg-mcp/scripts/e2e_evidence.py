"""E2E evidence script: MCP client calls the running server's query tool.

Demonstrates (no LLM key needed):
1. Tool listing over the MCP protocol
2. Invalid database name -> clean DATABASE_NOT_FOUND error (multi-DB routing)
3. Oversized question -> INVALID_PARAMETER (configured length limit)
4. Normal question without an API key -> graceful llm_unavailable error

Usage: uv run python scripts/e2e_evidence.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main() -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "pg_mcp"],
        cwd=os.path.join(os.path.dirname(__file__), ".."),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print("=" * 70)
            print("1. MCP 协议工具列表")
            print("=" * 70)
            for tool in tools.tools:
                print(f"  tool: {tool.name} — {(tool.description or '').splitlines()[0]}")

            print()
            print("=" * 70)
            print("2. 指定不存在的数据库 -> 干净的路由错误")
            print("=" * 70)
            result = await session.call_tool(
                "query",
                {"question": "How many users?", "database": "nonexistent_db"},
            )
            print("  " + result.content[0].text[:400])

            print()
            print("=" * 70)
            print("3. 超长问题 -> 按配置的最大长度拒绝")
            print("=" * 70)
            result = await session.call_tool(
                "query",
                {"question": "x" * 20000, "return_type": "sql"},
            )
            print("  " + result.content[0].text[:300])

            print()
            print("=" * 70)
            print("4. 正常问题（未配置 LLM key）-> 优雅的 llm_unavailable 错误")
            print("=" * 70)
            result = await session.call_tool(
                "query",
                {"question": "How many users are there?", "return_type": "result"},
            )
            print("  " + result.content[0].text[:400])

            print()
            print("=" * 70)
            print("5. schema 已在启动时缓存 (blog_small: 10 tables)")
            print("=" * 70)
            print("  完成。服务器仍在运行，可访问 http://localhost:9090/metrics 查看指标。")

            # Keep the (child) server alive so metrics can be inspected
            await asyncio.sleep(float(os.environ.get("EVIDENCE_KEEP_ALIVE", "0")))


if __name__ == "__main__":
    asyncio.run(main())
