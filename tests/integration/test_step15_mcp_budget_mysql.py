"""步骤15：MCP 客户端的真实 MySQL 调用预算跨实例生效。"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from bootstrap.settings import load_settings
from sqlalchemy import text

from application.common.context import TraceContext
from application.common.execution.invocation_scope import ToolInvocationPending, bind_tool_tenant
from infrastructure.mcp.client import MCPToolClient
from infrastructure.persistence.mysql.connection import create_mysql_engine
from infrastructure.persistence.mysql.repositories.tool_invocation_repository import (
    MySQLToolInvocationRepository,
)


class RecordedMCP(MCPToolClient):
    def __init__(self, repository, replies):
        super().__init__("http://unused.invalid", invocations=repository)
        self.replies = list(replies)
        self.calls = 0

    async def _call_once(self, tool_name, arguments, session_token, trace):
        self.calls += 1
        return self.replies.pop(0)


def test_client_reuses_success_and_keeps_retry_budget_after_restart() -> None:
    settings = load_settings()
    engine = create_mysql_engine(
        settings.mysql_url, settings.mysql_connect_timeout_s, settings.mysql_read_timeout_s
    )
    tenant = f"step15-mcp-{uuid.uuid4().hex[:12]}"
    repository = MySQLToolInvocationRepository(engine)
    trace = TraceContext("1.0", f"req-{uuid.uuid4().hex}", "corr-mcp", "trace-mcp")
    call = dict(
        tool_name="query_order",
        arguments={"order_id": "ORDER-01"},
        session_token="test-token",
        trace=trace,
        access="read",
    )
    try:
        first = RecordedMCP(
            repository,
            [
                {"status": "error", "error_code": "TIMEOUT"},
                {"status": "success", "data": {"order_id": "ORDER-01"}},
            ],
        )
        with bind_tool_tenant(tenant):
            answer = asyncio.run(first.call_tool(**call))
        assert first.calls == 2
        assert answer["status"] == "success"
        restarted = RecordedMCP(repository, [])
        with bind_tool_tenant(tenant):
            repeated = asyncio.run(restarted.call_tool(**call))
        assert repeated["data"]["order_id"] == "ORDER-01"
        assert restarted.calls == 0
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT attempt_count,retry_count,status "
                    "FROM tool_invocation WHERE tenant_id=:tenant"
                ),
                {"tenant": tenant},
            ).one()
        assert tuple(row) == (2, 1, "succeeded")
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM tool_invocation WHERE tenant_id=:tenant"), {"tenant": tenant}
            )
        engine.dispose()


def test_write_unknown_is_never_replayed_after_restart() -> None:
    settings = load_settings()
    engine = create_mysql_engine(
        settings.mysql_url, settings.mysql_connect_timeout_s, settings.mysql_read_timeout_s
    )
    tenant = f"step15-write-{uuid.uuid4().hex[:12]}"
    repository = MySQLToolInvocationRepository(engine)
    trace = TraceContext("1.0", f"req-{uuid.uuid4().hex}", "corr-mcp", "trace-mcp")
    call = dict(
        tool_name="submit_refund",
        arguments={"operation_id": "OP-01"},
        session_token="test-token",
        trace=trace,
        access="write",
    )
    try:
        first = RecordedMCP(repository, [{"status": "error", "error_code": "TIMEOUT"}])
        with bind_tool_tenant(tenant):
            assert asyncio.run(first.call_tool(**call))["error_code"] == "TIMEOUT"
        assert first.calls == 1
        restarted = RecordedMCP(repository, [])
        with bind_tool_tenant(tenant), pytest.raises(ToolInvocationPending):
            asyncio.run(restarted.call_tool(**call))
        assert restarted.calls == 0
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM tool_invocation WHERE tenant_id=:tenant"), {"tenant": tenant}
            )
        engine.dispose()
