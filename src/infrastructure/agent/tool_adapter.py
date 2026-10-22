"""工具执行端口适配器：把应用层端口桥接到 MCP 客户端（同步边界）。

编排层为同步执行；此处以 asyncio.run 桥接 MCP 客户端的异步实现。
统一只读重试预算在 MCPToolClient 内部唯一持有，适配器不叠加任何重试。
"""

from __future__ import annotations

import asyncio
from typing import Any

from application.common.context import TraceContext
from infrastructure.mcp.client import MCPToolClient


class MCPToolPort:
    """ToolExecutionPort 端口的生产实现。"""

    def __init__(self, client: MCPToolClient) -> None:
        self._client = client

    def execute_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        session_token: str,
        trace: TraceContext,
        access: str = "read",
    ) -> dict[str, Any]:
        return asyncio.run(
            self._client.call_tool(
                tool_name,
                arguments,
                session_token=session_token,
                trace=trace,
                access=access,
            )
        )
