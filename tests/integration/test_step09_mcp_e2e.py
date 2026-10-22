"""步骤09 MCP 端到端集成测试：独立进程 + 真实 MCP 客户端 + MySQL 审计落库。

覆盖验收矩阵：本人查询成功、越权/不存在统一 404 语义、支付未知 empty、
scope 缺失拒绝、未认证拒绝、tools/list 只暴露已实现工具、审计意图/完成
成对落库且令牌不落库。需要本地 docker MySQL（开发环境已启动）。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from bootstrap.settings import load_settings
from sqlalchemy import text

from application.common.context import TraceContext
from infrastructure.mcp.client import MCPToolClient
from infrastructure.persistence.mysql.connection import create_mysql_engine

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVER_START_TIMEOUT_S = 60.0


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class MCPServerProcess:
    def __init__(self, port: int, *, knowledge_enabled: bool = False) -> None:
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        log_file = tempfile.NamedTemporaryFile(
            prefix="mcp-e2e-", suffix=".log", delete=False, mode="w", encoding="utf-8"
        )
        self.log_path = log_file.name
        env = dict(os.environ)
        env.update(
            {
                "MCP_SERVER_HOST": "127.0.0.1",
                "MCP_SERVER_PORT": str(port),
                "PYTHONIOENCODING": "utf-8",
                "KNOWLEDGE_LOCAL_TOOLS_ENABLED": "true" if knowledge_enabled else "false",
            }
        )
        if not knowledge_enabled:
            # 步骤09的工具清单快照不依赖开发者本机后来开通的图谱账号。
            env["NEO4J_QUERY_PASSWORD"] = ""
        self.process = subprocess.Popen(
            [sys.executable, "-m", "bootstrap.mcp_main"],
            cwd=str(PROJECT_ROOT),
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )

    def wait_ready(self) -> None:
        deadline = time.monotonic() + SERVER_START_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"MCP 进程提前退出 code={self.process.returncode}，日志: "
                    f"{Path(self.log_path).read_text(encoding='utf-8', errors='replace')[-2000:]}"
                )
            try:
                response = httpx.get(f"{self.base}/health/live", timeout=1.0)
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.3)
        raise RuntimeError(
            f"MCP 进程 {SERVER_START_TIMEOUT_S}s 内未就绪，日志: "
            f"{Path(self.log_path).read_text(encoding='utf-8', errors='replace')[-2000:]}"
        )

    def login(self, account_id: str) -> str:
        response = httpx.post(
            f"{self.base}/auth/mock/session", json={"account_id": account_id}, timeout=5.0
        )
        assert response.status_code == 200, response.text
        return response.json()["data"]["session_token"]

    def call(self, tool_name: str, arguments: dict, *, token: str, trace: TraceContext) -> dict:
        client = MCPToolClient(f"{self.base}/mcp")
        import asyncio

        return asyncio.run(client.call_tool(tool_name, arguments, session_token=token, trace=trace))

    def stop(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)


@pytest.fixture(scope="module")
def server():
    process = MCPServerProcess(_free_port())
    try:
        process.wait_ready()
        yield process
    finally:
        process.stop()


@pytest.fixture(scope="module")
def audit_engine():
    settings = load_settings()
    engine = create_mysql_engine(
        settings.mysql_url, settings.mysql_connect_timeout_s, settings.mysql_read_timeout_s
    )
    yield engine
    # 只清理本测试写入的审计行（固定前缀），不动 mock 种子数据。
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM agent_audit_log WHERE request_id LIKE 'req-e2e-%'"))
    engine.dispose()


def _trace(tag: str) -> TraceContext:
    # 每次运行唯一：审计断言按精确 request_id 匹配，避免历史运行残留干扰。
    run = uuid.uuid4().hex[:8]
    return TraceContext(
        schema_version="1.0",
        request_id=f"req-e2e-{tag}-{run}",
        correlation_id=f"corr-e2e-{tag}-{run}",
        trace_id=f"trace-e2e-{tag}-{run}",
        thread_id=None,
    )


def _login(server, account_id: str) -> str:
    return server.login(account_id)


def test_tools_list_only_exposes_implemented_tools(server) -> None:
    import asyncio

    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def _list():
        async with streamablehttp_client(f"{server.base}/mcp", timeout=timedelta(seconds=10)) as (
            read_stream,
            write_stream,
            _,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                await asyncio.wait_for(session.initialize(), timeout=5.0)
                result = await asyncio.wait_for(session.list_tools(), timeout=5.0)
                return {tool.name for tool in result.tools}

    assert asyncio.run(_list()) == {
        "query_order",
        "query_payment",
        "query_tracking",
        "query_logistics",
        "check_delivery_exception",
        "check_refund_policy",
        "calculate_refund",
        "create_refund_request",
        "query_refund_request",
        "query_ticket",
        "create_ticket",
        "update_ticket",
        "handoff_to_human",
        "query_return_progress",
        "query_refund_execution",
        "query_after_sales_metrics",
        "explain_anomaly",
        "check_shipping_address_change",
        "query_shipping_address_change",
        "update_shipping_address",
    }


def test_owner_order_query_success(server) -> None:
    token = _login(server, "cust-north-01")
    trace = _trace("order-ok")
    envelope = server.call("query_order", {"order_id": "ORDER-001"}, token=token, trace=trace)
    assert envelope["status"] == "success", envelope
    assert envelope["error"] is None and envelope["error_code"] is None
    assert envelope["request_id"] == trace.request_id
    assert envelope["correlation_id"] == trace.correlation_id
    assert envelope["trace_id"] == trace.trace_id
    assert envelope["invocation_id"]
    order = envelope["data"]["order"]
    assert order == {
        "order_id": "ORDER-001",
        "owner_customer_id": "CUSTOMER-01",
        "status": "delivered",
        "payment_status": "succeeded",
        "fulfillment_status": "delivered",
        "currency": "CNY",
        "amount_minor": 100000,
    }
    assert envelope["data"]["snapshot_version"] == "mock-v2"
    assert envelope["parameter_summary"] == {"order_id": "ORDER-001"}


def test_cross_customer_order_denied_with_generic_404_semantics(server) -> None:
    token = _login(server, "cust-north-01")
    envelope = server.call(
        "query_order",
        {"order_id": "ORDER-999-OTHER"},
        token=token,
        trace=_trace("order-denied"),
    )
    assert envelope["status"] == "error"
    assert envelope["error_code"] == "RESOURCE_UNAVAILABLE"
    assert envelope["error"]["retryable"] is False
    # 泛化文案：不泄露存在性与归属细节。
    assert envelope["error"]["message"] == "资源不存在或不在授权范围内"


def test_other_users_order_same_denial(server) -> None:
    """越权（CUSTOMER-02 的订单）与不存在同一错误码与文案。"""
    token = _login(server, "cust-north-01")
    order_id = _customer02_order_id_engine()
    envelope = server.call(
        "query_order", {"order_id": order_id}, token=token, trace=_trace("order-cross")
    )
    assert envelope["status"] == "error"
    assert envelope["error_code"] == "RESOURCE_UNAVAILABLE"
    assert envelope["error"]["message"] == "资源不存在或不在授权范围内"


def _customer02_order_id_engine() -> str:
    settings = load_settings()
    from infrastructure.persistence.mysql.connection import create_mysql_engine as _mk

    engine = _mk(
        settings.mysql_url, settings.mysql_connect_timeout_s, settings.mysql_read_timeout_s
    )
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT order_id FROM mock_oms_order "
                    "WHERE tenant_id='tenant-north' AND customer_id='CUSTOMER-02' "
                    "ORDER BY order_id LIMIT 1"
                )
            ).first()
        assert row is not None
        return row.order_id
    finally:
        engine.dispose()


def test_owner_payment_query_success(server) -> None:
    token = _login(server, "cust-north-01")
    envelope = server.call(
        "query_payment", {"order_id": "ORDER-001"}, token=token, trace=_trace("pay-ok")
    )
    assert envelope["status"] == "success", envelope
    data = envelope["data"]
    assert data["order_id"] == "ORDER-001"
    assert data["payment_status"] == "succeeded"
    assert data["paid_minor"] == 100000
    assert data["refunded_minor"] == 10000
    assert data["payment_ids"] == ["PAY-001"]
    assert data["currency"] == "CNY"


def test_payment_unknown_maps_to_empty_status(server) -> None:
    """tenant-south/ORDER-001 无支付记录：empty + payment_status=unknown，而非错误。"""
    token = _login(server, "cust-south-91")
    envelope = server.call(
        "query_payment", {"order_id": "ORDER-001"}, token=token, trace=_trace("pay-unknown")
    )
    assert envelope["status"] == "empty", envelope
    assert envelope["error"] is None
    assert envelope["data"]["payment_status"] == "unknown"
    assert envelope["data"]["paid_minor"] == 0
    assert envelope["data"]["payment_ids"] == []


def test_missing_scope_denied(server) -> None:
    """admin-north-01 只有 audit:read：订单工具按统一安全语义拒绝。"""
    token = _login(server, "admin-north-01")
    envelope = server.call(
        "query_order", {"order_id": "ORDER-001"}, token=token, trace=_trace("scope-denied")
    )
    assert envelope["status"] == "error"
    assert envelope["error_code"] == "RESOURCE_UNAVAILABLE"
    assert envelope["error"]["retryable"] is False


def test_invalid_token_unauthenticated(server) -> None:
    envelope = server.call(
        "query_order",
        {"order_id": "ORDER-001"},
        token="not-a-real-token",
        trace=_trace("unauth"),
    )
    assert envelope["status"] == "error"
    assert envelope["error_code"] == "UNAUTHENTICATED"
    assert envelope["error"]["retryable"] is False
    assert envelope["error"]["message"] == "身份无效或会话已过期"


def test_audit_rows_persisted_and_token_never_persisted(server, audit_engine) -> None:
    """成功调用在审计表落一行终态 success（意图插入+完成条件更新两阶段合一行）；
    参数摘要不含令牌与会话信息。意图/完成两阶段的进程内语义由单元测试覆盖。"""
    token = _login(server, "cust-north-01")
    trace = _trace("audit-pair")
    envelope = server.call("query_order", {"order_id": "ORDER-001"}, token=token, trace=trace)
    assert envelope["status"] == "success"
    with audit_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT audit_id, status, action, parameter_summary, result_summary "
                "FROM agent_audit_log WHERE request_id = :r"
            ),
            {"r": trace.request_id},
        ).all()
    assert len(rows) == 1, [tuple(r) for r in rows]
    row = rows[0]
    assert row.status == "success"
    assert row.action == "tool.query_order"
    # 库内不落令牌与授权头；参数摘要只含脱敏后的 order_id。
    assert token not in (row.parameter_summary or "")
    assert token not in (row.result_summary or "")
    assert "Bearer" not in (row.parameter_summary or "")
    assert json.loads(row.parameter_summary) == {"order_id": "ORDER-001"}
    assert json.loads(row.result_summary) == {"status": "success"}
