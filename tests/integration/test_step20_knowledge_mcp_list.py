"""开发运行时四个知识工具实际注册；不激活合成发布或装载模型。"""

import asyncio
from datetime import timedelta

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from tests.integration.test_step09_mcp_e2e import MCPServerProcess, _free_port


def test_local_mcp_lists_all_four_knowledge_tools() -> None:
    process = MCPServerProcess(_free_port(), knowledge_enabled=True)
    try:
        process.wait_ready()

        async def list_tools():
            async with streamablehttp_client(
                f"{process.base}/mcp", timeout=timedelta(seconds=10)
            ) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await asyncio.wait_for(session.initialize(), timeout=5)
                    result = await asyncio.wait_for(session.list_tools(), timeout=5)
                    return {tool.name for tool in result.tools}

        names = asyncio.run(list_tools())
        assert {
            "search_knowledge_base",
            "query_knowledge_graph",
            "search_hybrid_knowledge",
            "get_policy_detail",
        } <= names
    finally:
        process.stop()
