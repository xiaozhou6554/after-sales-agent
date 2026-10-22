"""步骤09 工具执行器单元测试：鉴权→二次授权→审计→信封的完整服务端语义。"""

from __future__ import annotations

from datetime import datetime, timezone

from application.common.audit import AuditUseCase
from application.common.authorization import (
    GENERIC_DENIED_MESSAGE,
    AuthorizationDenied,
)
from application.common.context import AuthContext
from application.common.errors import ErrorCode
from application.common.ports.audit import AuditPersistenceError
from application.order.dto.order import payment_result
from application.order.use_cases.order_query import QueryOrderUseCase, QueryPaymentUseCase
from interface.mcp.context_guard import ToolContextGuard
from interface.mcp.tools import MCPToolDeps, execute_tool_call
from tests.unit.test_step09_order_use_case import NOW, FakeOrderPort, make_auth, make_snapshot

PROJECT_MCP_TOOLS = "data/contracts/mcp-tools.json"

HEADERS_SELF = {
    "authorization": "Bearer token-self",
    "x-request-id": "req-mcp-1",
    "x-correlation-id": "corr-mcp-1",
    "x-trace-id": "trace-mcp-1",
    "x-thread-id": "thread-mcp-1",
}


class FakeGateway:
    """令牌→AuthContext 的桩：以令牌后缀区分主体。"""

    def __init__(self, mapping: dict[str, AuthContext], fail: bool = False) -> None:
        self.mapping = mapping
        self.fail = fail

    def mock_login_enabled(self) -> bool:
        return False

    def issue_mock_session(self, account_id: str):
        raise AssertionError("工具执行不签发会话")

    def resolve(self, token: str) -> AuthContext:
        if self.fail:
            raise AuthorizationDenied(ErrorCode.UNAUTHENTICATED, "expired")
        return self.mapping[token]


class FakeGuard(ToolContextGuard):
    """参数校验复用真实注册表实现；scope 清单以桩替换，租户与有效期逻辑复用真实实现。"""

    def __init__(self, scopes: dict[str, tuple[str, ...]]) -> None:
        ToolContextGuard.__init__(self, PROJECT_MCP_TOOLS)
        self._scopes = scopes

    def verify(self, tool_name, auth, *, tenant_id=None, now=None) -> None:
        from application.common.authorization import verify_tool_context

        if tool_name not in self._scopes:
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
        verify_tool_context(auth, self._scopes[tool_name], tenant_id=tenant_id, now=now)


class RecordingSink:
    def __init__(self, fail_intent: bool = False) -> None:
        self.intents = []
        self.completions = []
        self.actions = []
        self.fail_intent = fail_intent

    def record_intent(self, intent) -> None:
        if self.fail_intent:
            raise AuditPersistenceError("db down")
        self.intents.append(intent)

    def append_result(self, completion) -> None:
        self.completions.append(completion)

    def record_action(self, action) -> None:
        self.actions.append(action)


def build_deps(
    orders: dict,
    payments: dict,
    *,
    gateway: FakeGateway | None = None,
    sink: RecordingSink | None = None,
    scopes: dict[str, tuple[str, ...]] | None = None,
) -> MCPToolDeps:
    port = FakeOrderPort(orders, payments)
    adapter_scopes = scopes or {"query_order": ("order:read",), "query_payment": ("payment:read",)}
    return MCPToolDeps(
        auth_gateway=gateway or FakeGateway({"token-self": make_auth()}),
        guard=FakeGuard(adapter_scopes),
        query_order=QueryOrderUseCase(port),
        query_payment=QueryPaymentUseCase(port),
        audit=AuditUseCase(sink or RecordingSink()),
        clock=lambda: NOW.replace(tzinfo=None),
    )


def test_self_order_success_envelope_shape() -> None:
    deps = build_deps({("tenant-north", "ORDER-001"): make_snapshot()}, {})
    envelope = execute_tool_call(
        "query_order", {"order_id": "ORDER-001"}, deps=deps, headers=HEADERS_SELF
    )
    assert envelope["status"] == "success"
    assert envelope["tool_name"] == "query_order"
    assert envelope["request_id"] == "req-mcp-1"
    assert envelope["trace_id"] == "trace-mcp-1"
    assert envelope["invocation_id"]
    assert envelope["error"] is None and envelope["error_code"] is None
    data = envelope["data"]
    assert data["order"] == {
        "order_id": "ORDER-001",
        "owner_customer_id": "CUSTOMER-01",
        "status": "delivered",
        "payment_status": "succeeded",
        "fulfillment_status": "delivered",
        "currency": "CNY",
        "amount_minor": 100000,
    }
    assert data["snapshot_version"] == "mock-v2"
    assert envelope["parameter_summary"] == {"order_id": "ORDER-001"}


def test_other_users_order_denied_envelope() -> None:
    orders = {("tenant-north", "ORDER-002"): make_snapshot("ORDER-002", "CUSTOMER-02")}
    deps = build_deps(orders, {})
    envelope = execute_tool_call(
        "query_order", {"order_id": "ORDER-002"}, deps=deps, headers=HEADERS_SELF
    )
    assert envelope["status"] == "error"
    assert envelope["error_code"] == "RESOURCE_UNAVAILABLE"
    assert envelope["error"]["retryable"] is False
    assert envelope["error"]["error_code"] == envelope["error_code"]
    assert "CUSTOMER-02" not in envelope["error"]["message"]


def test_absent_order_denied_envelope() -> None:
    deps = build_deps({}, {})
    envelope = execute_tool_call(
        "query_order", {"order_id": "ORDER-404"}, deps=deps, headers=HEADERS_SELF
    )
    assert envelope["status"] == "error"
    assert envelope["error_code"] == "RESOURCE_UNAVAILABLE"


def test_unauthenticated_envelope_without_subject_leak() -> None:
    deps = build_deps({}, {})
    envelope = execute_tool_call("query_order", {"order_id": "ORDER-001"}, deps=deps, headers={})
    assert envelope["error_code"] == "UNAUTHENTICATED"
    assert envelope["error"]["retryable"] is False


def test_missing_scope_denied_and_audited() -> None:
    """scope 缺失按统一安全语义拒绝，并落单条终态安全审计。"""
    sink = RecordingSink()
    gateway = FakeGateway({"token-self": make_auth(scopes=("ticket:read",))})
    deps = build_deps(
        {("tenant-north", "ORDER-001"): make_snapshot()}, {}, gateway=gateway, sink=sink
    )
    envelope = execute_tool_call(
        "query_order", {"order_id": "ORDER-001"}, deps=deps, headers=HEADERS_SELF
    )
    assert envelope["error_code"] == "RESOURCE_UNAVAILABLE"
    assert sink.intents == []  # 二次授权失败不进入执行意图
    assert len(sink.actions) == 1 and sink.actions[0].status == "denied"


def test_expired_session_maps_to_unauthenticated() -> None:
    expired = make_auth(expires_at=datetime(2026, 10, 19, tzinfo=timezone.utc))
    deps = build_deps({}, {}, gateway=FakeGateway({"token-self": expired}))
    envelope = execute_tool_call(
        "query_order", {"order_id": "ORDER-001"}, deps=deps, headers=HEADERS_SELF
    )
    assert envelope["error_code"] == "UNAUTHENTICATED"


def test_dependency_failure_envelope_is_retryable() -> None:
    deps = build_deps({}, {})
    deps.query_order._port.fail = True
    envelope = execute_tool_call(
        "query_order", {"order_id": "ORDER-001"}, deps=deps, headers=HEADERS_SELF
    )
    assert envelope["error_code"] == "DEPENDENCY_UNAVAILABLE"
    assert envelope["error"]["retryable"] is True


def test_audit_intent_then_completion_flow() -> None:
    sink = RecordingSink()
    deps = build_deps({("tenant-north", "ORDER-001"): make_snapshot()}, {}, sink=sink)
    execute_tool_call("query_order", {"order_id": "ORDER-001"}, deps=deps, headers=HEADERS_SELF)
    assert len(sink.intents) == 1 and sink.intents[0].status == "pending"
    assert sink.intents[0].action == "tool.query_order"
    assert sink.intents[0].trace_id == "trace-mcp-1"
    assert len(sink.completions) == 1 and sink.completions[0].status == "success"
    assert sink.completions[0].audit_id == sink.intents[0].audit_id


def test_audit_intent_failure_blocks_execution() -> None:
    sink = RecordingSink(fail_intent=True)
    deps = build_deps({("tenant-north", "ORDER-001"): make_snapshot()}, {}, sink=sink)
    envelope = execute_tool_call(
        "query_order", {"order_id": "ORDER-001"}, deps=deps, headers=HEADERS_SELF
    )
    assert envelope["error_code"] == "DEPENDENCY_UNAVAILABLE"
    assert sink.completions == []


def test_payment_unknown_maps_to_empty_status_with_dto() -> None:
    order = make_snapshot()
    result = payment_result(None, order)
    assert result.outcome == "unknown"


def test_payment_success_envelope() -> None:
    from domain.order.snapshots import PaymentSnapshot

    payment = PaymentSnapshot(
        tenant_id="tenant-north",
        order_id="ORDER-001",
        payment_status="succeeded",
        paid_minor=100000,
        refunded_minor=10000,
        currency="CNY",
        payment_ids=("PAY-001",),
        snapshot_version="mock-v2",
        observed_at=NOW,
    )
    deps = build_deps(
        {("tenant-north", "ORDER-001"): make_snapshot()},
        {("tenant-north", "ORDER-001"): payment},
    )
    envelope = execute_tool_call(
        "query_payment", {"order_id": "ORDER-001"}, deps=deps, headers=HEADERS_SELF
    )
    assert envelope["status"] == "success"
    assert envelope["data"]["payment_ids"] == ["PAY-001"]
    assert envelope["data"]["refunded_minor"] == 10000
    assert envelope["data"]["paid_minor"] == 100000


def test_guard_denial_message_is_generic() -> None:
    """未知工具不在 guard 清单内：统一 RESOURCE_UNAVAILABLE 且不泄露细节。"""
    deps = build_deps({}, {})
    envelope = execute_tool_call("unknown_tool", {}, deps=deps, headers=HEADERS_SELF)
    assert envelope["error_code"] == "RESOURCE_UNAVAILABLE"
    assert envelope["error"]["retryable"] is False
