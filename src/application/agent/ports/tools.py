"""工具执行端口（依赖倒置）：Agent 经 MCP 客户端访问业务工具，不直连仓储。

返回值为 9.4 工具响应信封 dict（status/data/error）；统一只读重试预算由
MCP 客户端实现层持有（1.4），编排层不得叠加第二次重试。
"""

from __future__ import annotations

from typing import Any, Protocol

from application.common.context import TraceContext


class ToolExecutionPort(Protocol):
    def execute_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        session_token: str,
        trace: TraceContext,
        access: str = "read",
    ) -> dict[str, Any]:
        """执行一次工具调用并返回工具响应信封；通道失败已折算为错误信封。"""
        ...
