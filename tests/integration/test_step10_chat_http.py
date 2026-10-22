"""步骤10 对话 HTTP 集成测试：真实 MCP HTTP 服务 + 真实 MySQL 审计 + Fake LLM。

覆盖验收口径：POST /chat 全链路（认证→白名单→MCP 工具→守卫→信封），
跨客户统一 404 语义，缺参数追问（needs_input 契约），白名单外不执行，
审计一次意图一次落库且令牌不落库。模型验证用 Fake 脚本（真实模型单独记录）。

拓扑说明：MCP 服务以 uvicorn 线程在本进程内启动（真实端口、真实 HTTP 传输），
认证网关实例与 API 应用共享——Mock 登录态是进程内字典，生产由企业 IdP/
共享会话存储承担该角色；若 MCP 以独立进程启动则无法复现共享登录态，
故此处的进程隔离由步骤09 的独立子进程 E2E 覆盖。需要本地 docker MySQL。
"""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from bootstrap.api_main import create_app
from bootstrap.mcp_main import build_mcp_app
from bootstrap.settings import load_settings
from fastapi.testclient import TestClient
from sqlalchemy import text

from application.agent.ports.llm import LLMResult, LLMToolCall
from application.agent.prompts import PromptCatalog
from application.agent.service import ChatService
from infrastructure.agent.memory_adapter import InMemorySessionRepository
from infrastructure.agent.tool_adapter import MCPToolPort
from infrastructure.mcp.client import MCPToolClient
from infrastructure.persistence.mysql.connection import create_mysql_engine

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVER_START_TIMEOUT_S = 60.0
PROMPTS = PromptCatalog.load(PROJECT_ROOT / "data" / "prompts")
REGISTRY_PATH = PROJECT_ROOT / "data" / "contracts" / "mcp-tools.json"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class InProcessMCPServer:
    """本进程内 uvicorn 线程承载 MCP 应用；暴露共享认证网关。"""

    def __init__(self, settings) -> None:
        self.app = build_mcp_app(settings)
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        config = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning")
        self.server = uvicorn.Server(config)
        # 非主线程不允许安装信号处理器（Windows 下直接崩溃）。
        self.server.install_signal_handlers = lambda: None
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def wait_ready(self) -> None:
        self.thread.start()
        deadline = time.monotonic() + SERVER_START_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.server.started:
                return
            if self.server.should_exit:
                raise RuntimeError("MCP 服务线程提前退出")
            time.sleep(0.1)
        raise RuntimeError(f"MCP 服务 {SERVER_START_TIMEOUT_S}s 内未就绪")

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=15)

    @property
    def auth_gateway(self):
        return self.app.state.auth


class QueueLLM:
    """测试可换脚本的 Fake LLM（app 装配一次，脚本逐用例替换）。"""

    def __init__(self) -> None:
        self.queue: list[LLMResult] = []
        self.calls: list[dict] = []

    def complete(self, *, system, messages, tools=None):
        self.calls.append(
            {"system": system, "messages": list(messages), "tools": list(tools or ())}
        )
        if not self.queue:
            raise AssertionError("FakeLLM 队列为空")
        return self.queue.pop(0)


@pytest.fixture(scope="module")
def server():
    mcp_server = InProcessMCPServer(load_settings())
    try:
        mcp_server.wait_ready()
        yield mcp_server
    finally:
        mcp_server.stop()


@pytest.fixture(scope="module")
def seen_request_ids():
    """记录触发过工具调用的请求 id，模块结束按 id 精确清理审计行。"""
    ids: list[str] = []
    yield ids


@pytest.fixture(scope="module")
def audit_engine(seen_request_ids):
    settings = load_settings()
    engine = create_mysql_engine(
        settings.mysql_url, settings.mysql_connect_timeout_s, settings.mysql_read_timeout_s
    )
    yield engine
    with engine.begin() as conn:
        for request_id in seen_request_ids:
            conn.execute(
                text("DELETE FROM agent_audit_log WHERE request_id = :rid"), {"rid": request_id}
            )
    engine.dispose()


@pytest.fixture(scope="module")
def llm():
    return QueueLLM()


@pytest.fixture(scope="module")
def client(server, llm):
    settings = load_settings()
    chat_service = ChatService(
        llm=llm,
        tools=MCPToolPort(MCPToolClient(f"{server.base}/mcp")),
        memory=InMemorySessionRepository(),
        prompts=PROMPTS,
        registry_path=REGISTRY_PATH,
    )
    app = create_app(settings, readiness=object(), chat_service=chat_service)
    # 与 MCP 应用共享同一认证网关实例（等价于生产的共享会话存储）。
    app.state.auth = server.auth_gateway
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def api_login(client):
    """经 API 网关登录（与生产一致：用户先向网关认证）。"""

    def _login(account_id: str) -> str:
        response = client.post("/auth/mock/session", json={"account_id": account_id})
        assert response.status_code == 200, response.text
        return response.json()["data"]["session_token"]

    return _login


def _order_dispatch(order_id: str | None) -> LLMResult:
    arguments = {"order_id": order_id} if order_id is not None else {}
    return LLMResult(
        tool_calls=(LLMToolCall(call_id="c1", name="query_order", arguments=arguments),),
        model="fake",
    )


def test_chat_without_token_is_unauthenticated(client) -> None:
    response = client.post("/chat", json={"message": "你好"})
    assert response.status_code == 401
    body = response.json()
    assert body["status"] == "error"
    assert body["error"]["error_code"] == "UNAUTHENTICATED"


def test_order_query_success_roundtrip(
    client, audit_engine, llm, seen_request_ids, api_login
) -> None:
    token = api_login("cust-north-01")
    llm.queue = [
        _order_dispatch("ORDER-001"),
        LLMResult(content="您的订单 ORDER-001 已送达，支付成功，金额 1000.00 CNY。", model="fake"),
    ]
    response = client.post(
        "/chat",
        json={"message": "帮我查一下订单 ORDER-001"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    seen_request_ids.append(body["request_id"])
    assert body["status"] == "success" and body["next_action"] == "finish", body
    assert "1000.00 CNY" in body["answer"], body
    # 链路三元组由服务端生成；请求体未携带任何链路/身份字段。
    assert body["request_id"].startswith("req-") and body["thread_id"].startswith("thread-")
    assert body["error"] is None
    # 审计一次意图一行、终态 success，令牌不落库。
    with audit_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT status, action, parameter_summary FROM agent_audit_log "
                "WHERE request_id = :rid AND action = 'tool.query_order'"
            ),
            {"rid": body["request_id"]},
        ).fetchall()
    assert len(row) == 1 and row[0].status == "success", row
    assert "Bearer" not in str(row[0].parameter_summary)
    assert token not in str(row[0].parameter_summary)


def test_logistics_query_in_one_customer_chat(
    client, audit_engine, llm, seen_request_ids, api_login
) -> None:
    """步骤22：物流工具内部验证订单归属，Agent 获得结构化轨迹摘要。"""
    token = api_login("cust-north-01")
    llm.queue = [
        LLMResult(
            tool_calls=(
                LLMToolCall(
                    call_id="c1", name="query_logistics", arguments={"order_id": "ORDER-001"}
                ),
            ),
            model="fake",
        ),
    ]
    calls_before = len(llm.calls)
    response = client.post(
        "/chat",
        json={"message": "订单 ORDER-001 到哪了？"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    seen_request_ids.append(body["request_id"])
    assert body["status"] == "success"
    assert "MOCK-TRACK-N-001" in body["answer"] and "delivered" in body["answer"]
    assert "预计送达" not in body["answer"]
    assert len(llm.calls) == calls_before + 1  # 不让模型重写物流事实
    with audit_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT action, status FROM agent_audit_log WHERE request_id=:rid "
                "AND action='tool.query_logistics'"
            ),
            {"rid": body["request_id"]},
        ).fetchall()
    assert len(rows) == 1 and rows[0].action == "tool.query_logistics"
    assert all(row.status == "success" for row in rows)


def test_conversation_memory_across_calls_in_same_thread(client, llm, api_login) -> None:
    token = api_login("cust-north-01")
    headers = {"Authorization": f"Bearer {token}"}
    llm.queue = [LLMResult(content="您好，请问有什么可以帮您？", model="fake")]
    first = client.post(
        "/chat", json={"message": "第一句", "thread_id": "thread-it-mem"}, headers=headers
    )
    assert first.status_code == 200 and first.json()["status"] == "success"
    llm.queue = [LLMResult(content="好的，我明白了。", model="fake")]
    second = client.post(
        "/chat", json={"message": "第二句", "thread_id": "thread-it-mem"}, headers=headers
    )
    assert second.status_code == 200
    messages = llm.calls[-1]["messages"]
    assert any(m.role == "user" and m.content == "第一句" for m in messages)
    greeting = "您好，请问有什么可以帮您？"
    assert any(m.role == "assistant" and m.content == greeting for m in messages)


def test_cross_customer_order_denied_with_generic_semantics(
    client, audit_engine, llm, seen_request_ids, api_login
) -> None:
    token = api_login("cust-north-01")
    with audit_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT order_id FROM mock_oms_order "
                "WHERE tenant_id = 'tenant-north' AND customer_id <> 'CUSTOMER-01' "
                "ORDER BY order_id LIMIT 1"
            )
        ).fetchone()
    assert row is not None
    llm.queue = [_order_dispatch(row.order_id)]
    response = client.post(
        "/chat",
        json={"message": f"帮我查一下订单 {row.order_id}"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    body = response.json()
    seen_request_ids.append(body["request_id"])
    assert body["status"] == "success"
    assert body["answer"] == "这个我暂时查不到，我帮您转接人工客服核实一下。"
    assert body["handoff_required"] is True and body["next_action"] == "handoff"


def test_missing_order_id_asks_user_with_contract_inputs(
    client, audit_engine, llm, seen_request_ids, api_login
) -> None:
    token = api_login("cust-north-01")
    llm.queue = [_order_dispatch(None)]  # 模型带空参数调用工具 → 预检缺参转追问
    response = client.post(
        "/chat",
        json={"message": "帮我查订单"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    seen_request_ids.append(body["request_id"])
    assert body["status"] == "needs_input" and body["next_action"] == "ask_user", body
    assert len(body["requested_inputs"]) == 1
    assert body["requested_inputs"][0]["field"] == "order_id"
    assert body["requested_inputs"][0]["prompt"] == "请提供订单号。"
    # 预检拦截：该请求不应有任何到达 MCP 的审计意图行。
    with audit_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT status FROM agent_audit_log WHERE request_id = :rid"),
            {"rid": body["request_id"]},
        ).fetchall()
    assert rows == []


def test_tool_outside_catalog_never_executed_for_scopeless_admin(
    client, audit_engine, llm, seen_request_ids, api_login
) -> None:
    token = api_login("admin-north-01")  # 仅 audit:read，目录为空
    llm.queue = [_order_dispatch("ORDER-001")]
    response = client.post(
        "/chat",
        json={"message": "帮我查订单 ORDER-001"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    body = response.json()
    seen_request_ids.append(body["request_id"])
    assert body["status"] == "success"
    assert body["answer"] == "这个问题需要人工进一步核实，我先为您申请转接人工服务。"
    assert body["handoff_required"] is True
    # 白名单外工具从未到达 MCP：该请求 id 下不应有任何审计意图行。
    with audit_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT status FROM agent_audit_log WHERE request_id = :rid"),
            {"rid": body["request_id"]},
        ).fetchall()
    assert rows == []


def test_customer_id_outside_data_scope_denied(client, api_login) -> None:
    token = api_login("cust-north-01")
    llm.queue = [LLMResult(content="不需要工具", model="fake")]
    response = client.post(
        "/chat",
        json={"message": "你好", "customer_id": "CUSTOMER-02"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["error_code"] == "RESOURCE_UNAVAILABLE"
    assert body["error"]["message"] == "资源不存在或不在授权范围内"
