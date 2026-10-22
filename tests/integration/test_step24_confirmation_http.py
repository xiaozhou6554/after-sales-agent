"""步骤24：HTTP 表单必须从可信会话绑定确认，拒绝客户端注入身份/摘要。"""

from __future__ import annotations

from bootstrap.api_main import create_app
from bootstrap.settings import load_settings
from fastapi.testclient import TestClient
from sqlalchemy import text

from application.common.operation_confirmation import OperationConfirmationUseCase, content_digest
from infrastructure.persistence.mysql.connection import create_mysql_engine
from infrastructure.security.confirmation_store import MySQLConfirmationStore


def test_ticket_confirmation_http_binds_trusted_actor_and_normalized_content() -> None:
    settings = load_settings()
    engine = create_mysql_engine(
        settings.mysql_url, settings.mysql_connect_timeout_s, settings.mysql_read_timeout_s
    )
    store = MySQLConfirmationStore(engine)
    app = create_app(
        settings=settings,
        readiness=object(),
        chat_service=object(),
        task_service=object(),
        confirmation_use_case=OperationConfirmationUseCase(store, 300),
    )
    confirmation_id = None
    try:
        with TestClient(app) as client:
            payload = {
                "schema_version": "1.0",
                "operation_type": "create",
                "content": {"title": "物流包装破损", "priority": "normal"},
            }
            assert client.post("/ticket-confirmations", json=payload).status_code == 401
            login = client.post("/auth/mock/session", json={"account_id": "cust-north-01"})
            assert login.status_code == 200
            token = login.json()["data"]["session_token"]
            headers = {"Authorization": f"Bearer {token}"}

            forged = client.post(
                "/ticket-confirmations",
                headers=headers,
                json={**payload, "customer_id": "CUSTOMER-02", "content_digest": "0" * 64},
            )
            assert forged.status_code == 400
            assert forged.json()["error"]["error_code"] == "INVALID_ARGUMENT"

            response = client.post("/ticket-confirmations", headers=headers, json=payload)
            assert response.status_code == 201
            data = response.json()["data"]["confirmation"]
            confirmation_id = data["confirmation_id"]
            assert data["content_digest"] == content_digest(payload["content"])
            assert "challenge" not in str(response.json()).lower()

        record = store.get("tenant-north", confirmation_id)
        assert record is not None
        assert record.status == "prepared"
        assert record.binding.tenant_id == "tenant-north"
        assert record.binding.actor_subject_id == "customer-north-01"
        assert record.binding.customer_id == "CUSTOMER-01"
        assert record.binding.confirmer_subject_id == "customer-north-01"
        assert record.binding.operation_type == "create"
        assert record.binding.target_id == "new"
        assert record.binding.payload_digest == content_digest(payload["content"])
        assert record.binding.expected_revision is None
    finally:
        if confirmation_id is not None:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "DELETE FROM operation_confirmation WHERE tenant_id=:tenant_id "
                        "AND confirmation_id=:confirmation_id"
                    ),
                    {"tenant_id": "tenant-north", "confirmation_id": confirmation_id},
                )
        engine.dispose()
