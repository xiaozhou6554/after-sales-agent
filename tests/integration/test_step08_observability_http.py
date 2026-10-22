"""步骤08 HTTP 可观测性集成测试：链路串联、失败留痕、敏感值不出现在日志。"""

from __future__ import annotations

import logging

from bootstrap.api_main import create_app
from bootstrap.settings import RuntimeSettings
from fastapi import FastAPI
from fastapi.testclient import TestClient


class ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def dev_app() -> FastAPI:
    return create_app(RuntimeSettings(app_env="development"))


def login(client: TestClient, account_id: str) -> dict:
    response = client.post("/auth/mock/session", json={"account_id": account_id})
    assert response.status_code == 200
    return response.json()


def test_access_log_carries_request_trace_and_metrics_records() -> None:
    app = dev_app()
    handler = ListHandler()
    root = logging.getLogger()
    root.addHandler(handler)
    previous_level = root.level
    root.setLevel(logging.INFO)
    try:
        client = TestClient(app)
        body = login(client, "cust-north-01")
        token = body["data"]["session_token"]
        me = client.get("/me", headers={"Authorization": f"Bearer {token}"}).json()
        # 一次请求一个 trace：访问日志与响应信封共用同一链路。
        access = [r for r in handler.records if r.name == "after_sales.access"]
        me_log = next(r for r in access if getattr(r, "trace_id", None) == me["trace_id"])
        assert me_log.request_id == me["request_id"]
        assert getattr(me_log, "subject_id", None) == "customer-north-01"
        assert getattr(me_log, "tenant_id", None) == "tenant-north"
        assert "duration_ms" in str(me_log.details)
        metrics = app.state.metrics.snapshot()
        assert metrics.counters["http_requests|method=GET,path=/me"] >= 1
        assert "http_latency_ms|method=GET,path=/me" in metrics.latencies
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)


def test_failures_are_logged_without_secrets() -> None:
    app = dev_app()
    handler = ListHandler()
    root = logging.getLogger()
    root.addHandler(handler)
    previous_level = root.level
    root.setLevel(logging.INFO)
    try:
        client = TestClient(app)
        secret_token = login(client, "cust-north-01")["data"]["session_token"]
        client.get("/me", headers={"Authorization": f"Bearer forged-{secret_token[:8]}"})
        client.post("/auth/mock/session", json={"account_id": "cust-north-01", "roles": ["admin"]})
        all_text = " ".join(handler.format(r) for r in handler.records)
        # 失败有记录：401 安全日志与 400 访问日志都在。
        assert any("authorization denied" in handler.format(r) for r in handler.records)
        assert any("POST /auth/mock/session -> 400" in handler.format(r) for r in handler.records)
        # 敏感值不出现：会话令牌不落日志。
        assert secret_token not in all_text
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)


def test_denied_access_logs_security_event_without_token() -> None:
    app = dev_app()
    handler = ListHandler()
    logging.getLogger().addHandler(handler)
    try:
        client = TestClient(app)
        client.get("/me")
        security = [r for r in handler.records if r.name == "after_sales.security"]
        assert security, "越权/未认证拒绝必须记录安全日志"
        assert "UNAUTHENTICATED" in handler.format(security[-1])
        assert "Bearer" not in handler.format(security[-1])
    finally:
        logging.getLogger().removeHandler(handler)
