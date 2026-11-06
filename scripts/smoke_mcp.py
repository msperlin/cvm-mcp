"""End-to-end smoke test over the real MCP stdio protocol.

Spawns ``python -m cvm_mcp``, initializes a session, lists the tools and calls
two of them. Catches packaging/registration problems that unit tests miss.

    uv run python scripts/smoke_mcp.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


async def main() -> int:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "cvm_mcp"],
        env={**os.environ, "CVMMCP_CACHE_DIR": os.environ.get("CVMMCP_CACHE_DIR", str(ROOT / ".cache"))},
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            info = await session.initialize()
            print(f"server: {info.server_info.name} {info.server_info.version}")

            tools = await session.list_tools()
            print(f"tools: {len(tools.tools)}")

            result = await session.call_tool("list_datasets", {})
            text = result.content[0].text
            print(f"list_datasets: {len(text)} chars, ok={'dfp' in text}")

            result = await session.call_tool("search_company", {"query": "vale", "limit": 1})
            text = result.content[0].text
            print(f"search_company: {'VALE S.A.' in text}")

            result = await session.call_tool(
                "get_statement_summary", {"cvm_code": 4170, "year": 2023}
            )
            text = result.content[0].text
            print(f"get_statement_summary: {'total_assets' in text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
