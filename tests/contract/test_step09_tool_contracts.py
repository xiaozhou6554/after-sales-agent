"""步骤09 工具契约测试：信封对齐 mcp-response.schema.json，注册表唯一源。

- 服务端产生的每种信封（成功/空/错误）都必须通过权威 JSON Schema 校验；
- 注册处理器与注册表输入 Schema 结构等价（忽略 title）；
- 处理器只覆盖注册表内工具，且 scope 以注册表为准。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from application.common.audit import AuditUseCase
from application.common.errors import ErrorCode
from application.order.use_cases.order_query import QueryOrderUseCase, QueryPaymentUseCase
from infrastructure.mcp.server_factory import build_order_tool_handlers, load_tool_registry
from interface.mcp.schemas.envelope import build_tool_response
from interface.mcp.tools import MCPToolDeps, execute_tool_call
from tests.unit.test_step09_order_use_case import NOW, FakeOrderPort, make_auth, make_snapshot
from tests.unit.test_step09_tool_execution import FakeGateway, FakeGuard, RecordingSink

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MCP_TOOLS_PATH = PROJECT_ROOT / "data" / "contracts" / "mcp-tools.json"
RESPONSE_SCHEMA_PATH = PROJECT_ROOT / "data" / "contracts" / "mcp-response.schema.json"

SCHEMA = json.loads(RESPONSE_SCHEMA_PATH.read_text(encoding="utf-8"))
REGISTRY = load_tool_registry(MCP_TOOLS_PATH)


def validate_envelope(envelope: dict) -> None:
    Draft202012Validator(SCHEMA).validate(envelope)


HEADERS = {
    "authorization": "Bearer t",
    "x-request-id": "req-ct-1",
    "x-correlation-id": "corr-ct-1",
    "x-trace-id": "trace-ct-1",
}


def build_deps(orders: dict, payments: dict) -> MCPToolDeps:
    port = FakeOrderPort(orders, payments)
    return MCPToolDeps(
        auth_gateway=FakeGateway({"t": make_auth()}),
        guard=FakeGuard({"query_order": ("order:read",), "query_payment": ("payment:read",)}),
        query_order=QueryOrderUseCase(port),
        query_payment=QueryPaymentUseCase(port),
        audit=AuditUseCase(RecordingSink()),
        clock=lambda: NOW.replace(tzinfo=None),
    )


def test_success_order_envelope_matches_json_schema() -> None:
    deps = build_deps({("tenant-north", "ORDER-001"): make_snapshot()}, {})
    envelope = execute_tool_call(
        "query_order", {"order_id": "ORDER-001"}, deps=deps, headers=HEADERS
    )
    validate_envelope(envelope)


def test_error_envelope_matches_json_schema() -> None:
    deps = build_deps({}, {})
    envelope = execute_tool_call(
        "query_order", {"order_id": "ORDER-404"}, deps=deps, headers=HEADERS
    )
    validate_envelope(envelope)


def test_empty_payment_envelope_matches_json_schema() -> None:
    from domain.order.snapshots import PaymentSnapshot

    payment = PaymentSnapshot(
        tenant_id="tenant-north",
        order_id="ORDER-001",
        payment_status="succeeded",
        paid_minor=100000,
        refunded_minor=0,
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
        "query_payment", {"order_id": "ORDER-001"}, deps=deps, headers=HEADERS
    )
    validate_envelope(envelope)
    assert envelope["status"] == "success"

    unknown_deps = build_deps({("tenant-north", "ORDER-001"): make_snapshot()}, {})
    deps_port = unknown_deps.query_payment._port
    deps_port.payments[("tenant-north", "ORDER-001")] = None
    empty = execute_tool_call(
        "query_payment", {"order_id": "ORDER-001"}, deps=unknown_deps, headers=HEADERS
    )
    validate_envelope(empty)
    assert empty["status"] == "empty"


def test_builder_rejects_inconsistent_error_shape() -> None:
    with pytest.raises(Exception):
        build_tool_response(
            request_id="req",
            correlation_id="corr",
            trace_id="trace",
            invocation_id="inv",
            tool_name="query_order",
            status="error",
            parameter_summary={},
            error_code=None,  # error 状态必须带 error_code
            message="x",
            retryable=False,
        )


def test_registry_covers_implemented_tools_with_scopes() -> None:
    assert {"query_order", "query_payment"} <= set(REGISTRY)
    assert REGISTRY["query_order"]["auth_scopes"] == ["order:read"]
    assert REGISTRY["query_payment"]["auth_scopes"] == ["payment:read"]
    assert REGISTRY["query_order"]["side_effect"] == "none"
    assert REGISTRY["query_payment"]["requires_confirmation"] is False


def _strip_nonstructural(node):
    """去掉 title 与约束关键字，只比较结构（约束由守卫运行时强制，见下一测试）。"""
    ignore = {"title", "minLength", "maxLength", "additionalProperties"}
    if isinstance(node, dict):
        return {k: _strip_nonstructural(v) for k, v in node.items() if k not in ignore}
    if isinstance(node, list):
        return [_strip_nonstructural(item) for item in node]
    return node


def test_fastmcp_tool_schemas_structurally_match_registry() -> None:
    """tools/list 的输入 Schema 与注册表结构一致。

    FastMCP 的 Schema 由纯签名生成（基础设施层禁用 pydantic），无法表达
    additionalProperties/minLength 约束；这些约束由守卫在每次调用时按
    注册表（唯一源）强制，见 test_registry_schema_enforced_at_runtime_by_guard。
    """
    from mcp.server.fastmcp.tools.base import Tool

    handlers = build_order_tool_handlers(object(), execute_tool_call)
    for name, handler in handlers.items():
        tool = Tool.from_function(handler, name=name)
        generated = _strip_nonstructural(tool.parameters)
        registry_schema = _strip_nonstructural(REGISTRY[name]["input_schema"])
        assert generated == registry_schema, name
        # 注册表自身必须声明封闭参数集（防止注册表意外放开）。
        assert REGISTRY[name]["input_schema"].get("additionalProperties") is False, name


def test_registry_schema_enforced_at_runtime_by_guard() -> None:
    """守卫按注册表 input_schema 校验模型可见参数（唯一源的运行时落点）。"""
    from application.common.authorization import AuthorizationDenied
    from interface.mcp.context_guard import ToolContextGuard

    guard = ToolContextGuard(MCP_TOOLS_PATH)
    guard.validate_arguments("query_order", {"order_id": "ORDER-001"})
    with pytest.raises(AuthorizationDenied) as empty:
        guard.validate_arguments("query_order", {"order_id": ""})
    assert empty.value.error_code is ErrorCode.INVALID_ARGUMENT
    with pytest.raises(AuthorizationDenied):
        guard.validate_arguments("query_order", {})
    with pytest.raises(AuthorizationDenied):
        guard.validate_arguments("query_order", {"order_id": "O", "extra": 1})


def test_create_server_rejects_unregistered_tool_names() -> None:
    from infrastructure.mcp.server_factory import create_mcp_server

    with pytest.raises(ValueError):
        create_mcp_server({"not_in_registry": lambda: None}, registry_path=MCP_TOOLS_PATH)


def test_auth_scope_source_is_registry_not_code() -> None:
    """guard 的 scope 必须来自注册表：真实 ToolContextGuard 逐次校验。"""
    from application.common.authorization import AuthorizationDenied
    from interface.mcp.context_guard import ToolContextGuard

    guard = ToolContextGuard(MCP_TOOLS_PATH)
    full = make_auth()  # order:read + payment:read
    guard.verify("query_order", full, tenant_id="tenant-north", now=NOW)
    guard.verify("query_payment", full, tenant_id="tenant-north", now=NOW)

    order_only = make_auth(scopes=("order:read",))
    with pytest.raises(AuthorizationDenied):
        guard.verify("query_payment", order_only, tenant_id="tenant-north", now=NOW)
    with pytest.raises(AuthorizationDenied):
        guard.verify("unknown_tool", full, tenant_id="tenant-north", now=NOW)
