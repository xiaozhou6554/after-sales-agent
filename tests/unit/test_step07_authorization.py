"""步骤07 授权策略单元测试：本人/代客/跨租户/过期/系统范围与工具上下文校验。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from application.common.authorization import (
    GENERIC_DENIED_MESSAGE,
    AuthorizationDenied,
    has_customer_access,
    identity_summary,
    require_all_scopes,
    require_customer_access,
    require_scope,
    require_tenant,
    verify_tool_context,
)
from application.common.context import AuthContext, DataScope
from application.common.errors import ErrorCode

NOW = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)


def make_auth(
    tenant_id="tenant-north",
    subject_id="customer-north-01",
    roles=("customer",),
    scopes=("order:read", "ticket:create"),
    customer_ids=("CUSTOMER-01",),
    team_ids=(),
    issued_at=NOW,
    expires_at=NOW + timedelta(hours=1),
) -> AuthContext:
    return AuthContext(
        tenant_id=tenant_id,
        subject_id=subject_id,
        roles=roles,
        scopes=scopes,
        data_scope=DataScope(customer_ids=customer_ids, team_ids=team_ids),
        session_id="sess-1",
        issued_at=issued_at,
        expires_at=expires_at,
    )


def test_customer_self_access_allowed() -> None:
    auth = make_auth()
    require_customer_access(auth, "CUSTOMER-01", tenant_id="tenant-north", now=NOW)
    assert has_customer_access(auth, "CUSTOMER-01")


def test_customer_cross_user_denied_with_unified_404() -> None:
    auth = make_auth()
    with pytest.raises(AuthorizationDenied) as exc:
        require_customer_access(auth, "CUSTOMER-02", tenant_id="tenant-north", now=NOW)
    assert exc.value.error_code is ErrorCode.RESOURCE_UNAVAILABLE
    assert exc.value.http_status == 404
    # 统一安全语义：越权与不存在使用同一泛化文案，不泄露资源存在性。
    assert str(exc.value) == GENERIC_DENIED_MESSAGE


def test_cross_tenant_access_denied() -> None:
    auth = make_auth()
    with pytest.raises(AuthorizationDenied) as exc:
        require_customer_access(auth, "CUSTOMER-91", tenant_id="tenant-south", now=NOW)
    assert exc.value.error_code is ErrorCode.RESOURCE_UNAVAILABLE
    with pytest.raises(AuthorizationDenied):
        require_tenant(auth, "tenant-south", now=NOW)


def test_support_on_behalf_within_allowed_scope_only() -> None:
    auth = make_auth(
        subject_id="support-north-01",
        roles=("support",),
        scopes=("customer:read", "ticket:read"),
        customer_ids=("CUSTOMER-01", "CUSTOMER-02"),
        team_ids=("team-north-support",),
    )
    require_customer_access(auth, "CUSTOMER-01", now=NOW)
    require_customer_access(auth, "CUSTOMER-02", now=NOW)
    with pytest.raises(AuthorizationDenied):
        require_customer_access(auth, "CUSTOMER-05", now=NOW)
    # 同租户不等于同团队：support 无 '*' 标记时不能凭租户获得范围。
    assert not has_customer_access(auth, "CUSTOMER-91")


def test_system_scope_marker_grants_tenant_wide_access() -> None:
    auth = make_auth(
        subject_id="ops-north-01",
        roles=("operations",),
        scopes=("incident:read",),
        customer_ids=("*",),
    )
    assert has_customer_access(auth, "CUSTOMER-91")
    require_customer_access(auth, "SYN-CUSTOMER-N-01-007", now=NOW)


def test_missing_scope_denied_and_expired_identity_unauthenticated() -> None:
    auth = make_auth(scopes=("order:read",))
    with pytest.raises(AuthorizationDenied) as scope_exc:
        require_scope(auth, "refund:review", now=NOW)
    assert scope_exc.value.error_code is ErrorCode.RESOURCE_UNAVAILABLE

    with pytest.raises(AuthorizationDenied) as all_exc:
        require_all_scopes(auth, ("order:read", "ticket:assign"), now=NOW)
    assert all_exc.value.error_code is ErrorCode.RESOURCE_UNAVAILABLE

    expired = make_auth(expires_at=NOW - timedelta(seconds=1))
    with pytest.raises(AuthorizationDenied) as auth_exc:
        require_scope(expired, "order:read", now=NOW)
    assert auth_exc.value.error_code is ErrorCode.UNAUTHENTICATED
    assert auth_exc.value.http_status == 401


def test_verify_tool_context_checks_expiry_tenant_and_scopes() -> None:
    auth = make_auth(scopes=("order:read", "payment:read"))
    verify_tool_context(auth, ("order:read",), tenant_id="tenant-north", now=NOW)

    with pytest.raises(AuthorizationDenied) as scope_exc:
        verify_tool_context(auth, ("order:read", "audit:read"), now=NOW)
    assert scope_exc.value.error_code is ErrorCode.RESOURCE_UNAVAILABLE

    with pytest.raises(AuthorizationDenied) as tenant_exc:
        verify_tool_context(auth, ("order:read",), tenant_id="tenant-south", now=NOW)
    assert tenant_exc.value.error_code is ErrorCode.RESOURCE_UNAVAILABLE

    expired = make_auth(expires_at=NOW - timedelta(seconds=1))
    with pytest.raises(AuthorizationDenied) as auth_exc:
        verify_tool_context(expired, ("order:read",), now=NOW)
    assert auth_exc.value.error_code is ErrorCode.UNAUTHENTICATED


def test_identity_summary_lists_grants_without_session_token() -> None:
    auth = make_auth(
        roles=("support",),
        scopes=("customer:read",),
        customer_ids=("CUSTOMER-01",),
        team_ids=("team-north-support",),
    )
    summary = identity_summary(auth)
    assert summary["tenant_id"] == "tenant-north"
    assert summary["subject_id"] == "customer-north-01"
    assert summary["roles"] == ["support"]
    assert summary["data_scope"] == {
        "team_ids": ["team-north-support"],
        "customer_ids": ["CUSTOMER-01"],
    }
    # 摘要不含会话令牌或可改写身份字段。
    assert "session_token" not in summary
    assert set(summary) == {
        "tenant_id",
        "subject_id",
        "roles",
        "scopes",
        "data_scope",
        "session_expires_at",
    }
