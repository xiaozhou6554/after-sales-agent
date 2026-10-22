"""步骤07 MockAuthAdapter 单元测试：签发、解析、过期、关闭与防伪造。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from bootstrap.container import MOCK_IDENTITIES_PATH

from application.common.authorization import (
    AuthorizationDenied,
    MockLoginDisabled,
)
from application.common.errors import ErrorCode
from infrastructure.auth.mock_auth_adapter import MockAuthAdapter, load_mock_identities

T0 = datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


def make_adapter(**kwargs) -> tuple[MockAuthAdapter, FakeClock]:
    clock = FakeClock()
    adapter = MockAuthAdapter.from_fixture(
        MOCK_IDENTITIES_PATH, enabled=True, session_ttl_s=3600.0, clock=clock, **kwargs
    )
    return adapter, clock


def test_issue_and_resolve_roundtrip_preserves_server_issued_identity() -> None:
    adapter, _ = make_adapter()
    grant = adapter.issue_mock_session("cust-north-01")
    assert grant.session_token
    auth = adapter.resolve(grant.session_token)
    assert auth.tenant_id == "tenant-north"
    assert auth.subject_id == "customer-north-01"
    assert auth.roles == ("customer",)
    assert auth.scopes == load_mock_identities(MOCK_IDENTITIES_PATH)["cust-north-01"].scopes
    assert auth.is_valid_at(T0 + timedelta(minutes=59))
    # session_id 与会话令牌分离：令牌泄露不等于身份字段可改写。
    assert auth.session_id != grant.session_token


def test_each_issue_creates_independent_session() -> None:
    adapter, _ = make_adapter()
    first = adapter.issue_mock_session("cust-north-01")
    second = adapter.issue_mock_session("cust-north-01")
    assert first.session_token != second.session_token
    assert first.auth.session_id != second.auth.session_id
    assert adapter.resolve(first.session_token).subject_id == "customer-north-01"
    assert adapter.resolve(second.session_token).subject_id == "customer-north-01"


def test_unknown_account_rejected_as_invalid_argument() -> None:
    adapter, _ = make_adapter()
    with pytest.raises(AuthorizationDenied) as exc:
        adapter.issue_mock_session("hacker-account")
    assert exc.value.error_code is ErrorCode.INVALID_ARGUMENT
    assert exc.value.http_status == 400


def test_client_cannot_inject_roles_or_scopes() -> None:
    # 签发签名只接受 account_id：角色/scope/数据范围没有任何客户端入口。
    import inspect

    signature = inspect.signature(MockAuthAdapter.issue_mock_session)
    assert list(signature.parameters) == ["self", "account_id"]
    adapter, _ = make_adapter()
    grant = adapter.issue_mock_session("admin-north-01")
    # admin 的权限严格来自 fixture（仅 audit:read），不因请求而扩大。
    assert grant.auth.roles == ("admin",)
    assert grant.auth.scopes == ("audit:read",)


def test_expired_session_resolves_as_unauthenticated() -> None:
    adapter, clock = make_adapter()
    grant = adapter.issue_mock_session("support-north-01")
    clock.now = T0 + timedelta(seconds=3601)
    with pytest.raises(AuthorizationDenied) as exc:
        adapter.resolve(grant.session_token)
    assert exc.value.error_code is ErrorCode.UNAUTHENTICATED
    assert exc.value.http_status == 401


def test_unknown_token_is_unauthenticated() -> None:
    adapter, _ = make_adapter()
    with pytest.raises(AuthorizationDenied) as exc:
        adapter.resolve("forged-token")
    assert exc.value.error_code is ErrorCode.UNAUTHENTICATED


def test_session_resolves_across_adapter_instances_same_fixture() -> None:
    # 跨进程拓扑等价验证：两个独立适配器实例共享同一 fixture（与生产
    # "网关签发、MCP 服务核验"的多进程部署一致），令牌必须互认。
    issuer, _ = make_adapter()
    verifier, _ = make_adapter()
    grant = issuer.issue_mock_session("cust-north-01")
    auth = verifier.resolve(grant.session_token)
    assert auth.tenant_id == "tenant-north"
    assert auth.subject_id == "customer-north-01"
    assert auth.scopes == grant.auth.scopes
    assert auth.session_id == grant.auth.session_id
    assert auth.is_valid_at(T0 + timedelta(minutes=59))


def test_token_signed_with_different_secret_is_unauthenticated() -> None:
    clock = FakeClock()
    issuer = MockAuthAdapter.from_fixture(
        MOCK_IDENTITIES_PATH, enabled=True, clock=clock, token_secret="dev-secret-a"
    )
    verifier = MockAuthAdapter.from_fixture(
        MOCK_IDENTITIES_PATH, enabled=True, clock=clock, token_secret="dev-secret-b"
    )
    grant = issuer.issue_mock_session("cust-north-01")
    with pytest.raises(AuthorizationDenied) as exc:
        verifier.resolve(grant.session_token)
    assert exc.value.error_code is ErrorCode.UNAUTHENTICATED


def test_disabled_adapter_refuses_issue_but_reports_closed() -> None:
    clock = FakeClock()
    adapter = MockAuthAdapter.from_fixture(MOCK_IDENTITIES_PATH, enabled=False, clock=clock)
    assert adapter.mock_login_enabled() is False
    with pytest.raises(MockLoginDisabled) as exc:
        adapter.issue_mock_session("cust-north-01")
    assert exc.value.error_code is ErrorCode.RESOURCE_UNAVAILABLE
    assert exc.value.http_status == 404


def test_fixture_accounts_all_issue_valid_contexts() -> None:
    adapter, clock = make_adapter()
    for account_id in load_mock_identities(MOCK_IDENTITIES_PATH):
        grant = adapter.issue_mock_session(account_id)
        auth = adapter.resolve(grant.session_token)
        assert auth.tenant_id and auth.subject_id
        assert auth.expires_at > clock.now
