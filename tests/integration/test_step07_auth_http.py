"""步骤07 HTTP 集成测试：Mock 登录、GET /me、统一错误信封与生产关闭。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from bootstrap.api_main import create_app
from bootstrap.settings import RuntimeSettings
from fastapi import FastAPI
from fastapi.testclient import TestClient


def dev_app() -> FastAPI:
    return create_app(RuntimeSettings(app_env="development"))


def prod_app():
    # 生产校验要求私有模型端点、API Key 与独立图谱只读凭据。
    return create_app(
        RuntimeSettings(
            app_env="production",
            llm_api_key="test-key",
            llm_base_url="http://private-llm.internal/v1",
            neo4j_query_password="test-only-reader-secret",
        )
    )


def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def login(client: TestClient, account_id: str) -> dict:
    response = client.post("/auth/mock/session", json={"account_id": account_id})
    assert response.status_code == 200, response.text
    return response.json()


# ---------- Mock 登录 ----------


def test_mock_login_issues_server_signed_session() -> None:
    client = TestClient(dev_app())
    body = login(client, "cust-north-01")
    assert body["status"] == "success"
    assert body["error"] is None
    data = body["data"]
    assert data["token_type"] == "bearer"
    assert data["session_token"]
    assert data["subject_id"] == "customer-north-01"
    assert data["roles"] == ["customer"]
    # 服务端签发：请求体之外的任何字段都不影响身份。
    assert body["request_id"] and body["trace_id"]


def test_mock_login_rejects_client_supplied_roles_scopes_and_identity() -> None:
    client = TestClient(dev_app())
    for extra in (
        {"roles": ["admin"]},
        {"scopes": ["audit:read"]},
        {"tenant_id": "tenant-south"},
        {"subject_id": "someone-else"},
        {"allowed_customer_ids": ["CUSTOMER-91"]},
    ):
        response = client.post("/auth/mock/session", json={"account_id": "cust-north-01", **extra})
        assert response.status_code == 400, extra
        payload = response.json()
        assert payload["status"] == "error"
        assert payload["error"]["error_code"] == "INVALID_ARGUMENT"


def test_mock_login_rejects_unknown_account() -> None:
    client = TestClient(dev_app())
    response = client.post("/auth/mock/session", json={"account_id": "hacker-01"})
    assert response.status_code == 400
    assert response.json()["error"]["error_code"] == "INVALID_ARGUMENT"


# ---------- GET /me 与认证中间件 ----------


def test_me_returns_identity_for_valid_session() -> None:
    client = TestClient(dev_app())
    token = login(client, "support-north-01")["data"]["session_token"]
    response = client.get("/me", headers=auth_header(token))
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    data = body["data"]
    assert data["subject_id"] == "support-north-01"
    assert data["roles"] == ["support"]
    assert "team-north-support" in data["data_scope"]["team_ids"]
    assert "CUSTOMER-01" in data["data_scope"]["customer_ids"]
    # 不返回会话令牌本身。
    assert "session_token" not in data


def test_me_requires_bearer_token() -> None:
    client = TestClient(dev_app())
    response = client.get("/me")
    assert response.status_code == 401
    payload = response.json()
    assert payload["status"] == "error"
    assert payload["error"]["error_code"] == "UNAUTHENTICATED"
    assert payload["error"]["retryable"] is False


def test_me_rejects_forged_and_malformed_tokens() -> None:
    client = TestClient(dev_app())
    for headers in (
        auth_header("forged-token"),
        {"Authorization": "Basic abc"},
        {"Authorization": "Bearer"},
    ):
        response = client.get("/me", headers=headers)
        assert response.status_code == 401, headers
        assert response.json()["error"]["error_code"] == "UNAUTHENTICATED"


def test_error_envelopes_carry_server_generated_trace() -> None:
    client = TestClient(dev_app())
    body = client.get("/me").json()
    assert body["request_id"].startswith("req-")
    assert body["correlation_id"].startswith("corr-")
    assert body["trace_id"].startswith("trace-")
    error = body["error"]
    assert error["request_id"] == body["request_id"]
    assert error["details"] == {"field_errors": []}


def test_expired_session_is_unauthenticated_over_http() -> None:
    client = TestClient(dev_app())
    token = login(client, "cust-north-01")["data"]["session_token"]
    # 直接操纵进程内 Mock 会话的有效期，模拟时间流逝。
    adapter = client.app.state.auth
    context = adapter._sessions[token]
    object.__setattr__(context, "expires_at", datetime(2020, 1, 1, tzinfo=timezone.utc))
    response = client.get("/me", headers=auth_header(token))
    assert response.status_code == 401
    assert response.json()["error"]["error_code"] == "UNAUTHENTICATED"


# ---------- 生产关闭 ----------


def test_mock_login_route_absent_in_production() -> None:
    app = prod_app()
    client = TestClient(app)
    response = client.post("/auth/mock/session", json={"account_id": "cust-north-01"})
    assert response.status_code == 404
    # 网关组件层面的第二道防线：适配器在生产拒绝签发。
    assert app.state.auth.mock_login_enabled() is False
    from application.common.authorization import MockLoginDisabled

    with pytest.raises(MockLoginDisabled):
        app.state.auth.issue_mock_session("cust-north-01")


def test_production_still_allows_health_endpoints() -> None:
    client = TestClient(prod_app())
    assert client.get("/health/live").json() == {"status": "alive"}
