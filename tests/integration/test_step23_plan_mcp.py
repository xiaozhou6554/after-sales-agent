"""步骤23：脚本化Planner经真实MCP依次查询订单、物流，不触发写工具。"""

from __future__ import annotations

import uuid

from bootstrap.container import MCP_TOOLS_PATH, PROMPTS_PATH, build_auth_gateway
from bootstrap.settings import load_settings
from sqlalchemy import text

from application.agent.ports.llm import LLMResult, LLMToolCall
from application.agent.prompts import PromptCatalog
from application.agent.service import ChatService
from application.common.context import TraceContext
from application.common.dto import ChatRequestDto
from infrastructure.agent.memory_adapter import InMemorySessionRepository
from infrastructure.agent.tool_adapter import MCPToolPort
from infrastructure.mcp.client import MCPToolClient
from infrastructure.persistence.mysql.connection import create_mysql_engine
from tests.integration.test_step09_mcp_e2e import MCPServerProcess, _free_port
from tests.unit.test_step10_agent_application import ScriptedLLM


def test_plan_order_then_logistics_through_real_mcp() -> None:
    settings = load_settings()
    process = MCPServerProcess(_free_port())
    request_id = f"req-step23-{uuid.uuid4().hex[:12]}"
    try:
        process.wait_ready()
        token = process.login("cust-north-01")
        auth = build_auth_gateway(settings).resolve(token)
        llm = ScriptedLLM(
            LLMResult(
                tool_calls=(
                    LLMToolCall("order", "query_order", {"order_id": "ORDER-001"}),
                    LLMToolCall("logistics", "query_logistics", {"order_id": "ORDER-001"}),
                )
            )
        )
        service = ChatService(
            llm=llm,
            tools=MCPToolPort(MCPToolClient(f"{process.base}/mcp")),
            memory=InMemorySessionRepository(),
            prompts=PromptCatalog.load(PROMPTS_PATH),
            registry_path=str(MCP_TOOLS_PATH),
        )
        trace = TraceContext(
            schema_version="1.0",
            request_id=request_id,
            correlation_id=f"corr-{request_id}",
            trace_id=f"trace-{request_id}",
        )
        response = service.execute(
            ChatRequestDto(trace=trace, message="查询 ORDER-001 的订单和物流"),
            auth,
            session_token=token,
        )
        assert response.status == "success"
        assert "ORDER-001" in response.answer
        assert "物流" in response.answer
        assert len(llm.calls) == 1
    finally:
        process.stop()
        engine = create_mysql_engine(
            settings.mysql_url,
            settings.mysql_connect_timeout_s,
            settings.mysql_read_timeout_s,
        )
        try:
            with engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM agent_audit_log WHERE request_id=:request_id"),
                    {"request_id": request_id},
                )
        finally:
            engine.dispose()
