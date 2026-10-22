"""步骤07 MCP 上下文守卫单测：逐次校验，不因连接来自 API 而跳过最终用户授权。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from application.common.authorization import AuthorizationDenied
from application.common.context import AuthContext, DataScope
from application.common.errors import ErrorCode
from interface.mcp.context_guard import ToolContextGuard

NOW = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)


def make_auth(scopes, expires_at=NOW + timedelta(hours=1)) -> AuthContext:
    return AuthContext(
        tenant_id="tenant-north",
        subject_id="subject-x",
        roles=("customer",),
        scopes=scopes,
        data_scope=DataScope(customer_ids=("CUSTOMER-01",), team_ids=()),
        session_id="sess-1",
        issued_at=NOW,
        expires_at=expires_at,
    )


@pytest.fixture()
def guard() -> ToolContextGuard:
    return ToolContextGuard("data/contracts/mcp-tools.json")


def test_manifest_tools_have_required_scopes(guard: ToolContextGuard) -> None:
    assert guard.required_scopes("query_order") == ("order:read",)
    assert guard.required_scopes("create_refund_request") == ("refund:request",)
    assert guard.required_scopes("explain_anomaly") == ("incident:read", "analytics:read")


def test_valid_context_passes_per_call(guard: ToolContextGuard) -> None:
    auth = make_auth(("order:read",))
    guard.verify("query_order", auth, tenant_id="tenant-north", now=NOW)


def test_missing_scope_denied_unified_404(guard: ToolContextGuard) -> None:
    auth = make_auth(("ticket:create",))
    with pytest.raises(AuthorizationDenied) as exc:
        guard.verify("query_order", auth, now=NOW)
    assert exc.value.error_code is ErrorCode.RESOURCE_UNAVAILABLE
    assert exc.value.http_status == 404


def test_unknown_tool_denied(guard: ToolContextGuard) -> None:
    auth = make_auth(("audit:read",))
    with pytest.raises(AuthorizationDenied):
        guard.verify("drop_database", auth, now=NOW)


def test_expired_context_is_unauthenticated_even_for_known_tool(
    guard: ToolContextGuard,
) -> None:
    auth = make_auth(("order:read",), expires_at=NOW - timedelta(seconds=1))
    with pytest.raises(AuthorizationDenied) as exc:
        guard.verify("query_order", auth, now=NOW)
    assert exc.value.error_code is ErrorCode.UNAUTHENTICATED


def test_cross_tenant_call_denied(guard: ToolContextGuard) -> None:
    auth = make_auth(("order:read",))
    with pytest.raises(AuthorizationDenied) as exc:
        guard.verify("query_order", auth, tenant_id="tenant-south", now=NOW)
    assert exc.value.error_code is ErrorCode.RESOURCE_UNAVAILABLE
