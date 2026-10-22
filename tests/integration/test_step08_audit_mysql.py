"""步骤08 MySQL 审计仓储集成测试：真实库上的意图/结果追加与唯一约束。

需要本地 docker MySQL（开发环境已启动）；测试只使用 tenant-step08 隔离数据，
结束后清理本测试写入的行，不影响 mock 种子数据。
"""

from __future__ import annotations

import json

import pytest
from bootstrap.migrate import build_executor, run_apply
from bootstrap.settings import load_settings
from sqlalchemy import text

from application.common.audit import AuditUseCase
from application.common.ports.audit import AuditPersistenceError
from infrastructure.observability.audit_repository import MySQLAuditRepository
from infrastructure.persistence.mysql.connection import create_mysql_engine

TEST_TENANT = "tenant-step08"


@pytest.fixture()
def audit_case():
    settings = load_settings()
    run_apply(build_executor(settings))  # 显式迁移：幂等，未应用则应用 v0003
    engine = create_mysql_engine(
        settings.mysql_url, settings.mysql_connect_timeout_s, settings.mysql_read_timeout_s
    )
    repository = MySQLAuditRepository(engine)
    use_case = AuditUseCase(repository)
    yield use_case, engine
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM agent_audit_log WHERE tenant_id = :t"), {"t": TEST_TENANT})
    engine.dispose()


def _ids() -> dict:
    return {
        "tenant_id": TEST_TENANT,
        "actor_subject_id": "subject-it",
        "action": "tool.query_order",
        "request_id": "req-step08",
        "correlation_id": "corr-step08",
        "trace_id": "trace-step08",
    }


def test_intent_and_result_roundtrip_in_mysql(audit_case) -> None:
    use_case, engine = audit_case
    audit_id = use_case.begin_tool_call(
        **_ids(),
        customer_id="CUSTOMER-01",
        resource_refs=("order:ORDER-001",),
        parameter_summary={"order_id": "ORDER-001", "phone": "13812345678"},
    )
    use_case.complete_tool_call(
        audit_id=audit_id,
        tenant_id=TEST_TENANT,
        result_summary={"status": "delivered", "raw": "邮箱 name@example.com"},
        duration_ms=8,
        status="success",
    )
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT status, parameter_summary, result_summary, duration_ms "
                "FROM agent_audit_log WHERE tenant_id=:t AND audit_id=:a"
            ),
            {"t": TEST_TENANT, "a": audit_id},
        ).one()
    assert row.status == "success"
    params = json.loads(row.parameter_summary)
    result = json.loads(row.result_summary)
    # 库内内容已脱敏：明文电话/邮箱不存在。
    assert params["phone"] == "***"
    assert "13812345678" not in row.parameter_summary
    assert "name@example.com" not in row.result_summary
    assert result["status"] == "delivered"
    assert row.duration_ms == 8


def test_completed_intent_cannot_be_completed_again(audit_case) -> None:
    use_case, _ = audit_case
    audit_id = use_case.begin_tool_call(**_ids(), parameter_summary={})
    use_case.complete_tool_call(audit_id=audit_id, tenant_id=TEST_TENANT, status="success")
    with pytest.raises(AuditPersistenceError):
        use_case.complete_tool_call(audit_id=audit_id, tenant_id=TEST_TENANT, status="failed")


def test_event_id_unique_per_tenant(audit_case) -> None:
    use_case, _ = audit_case
    use_case.record_action(**_ids(), status="success", event_id="evt-step08-1")
    with pytest.raises(AuditPersistenceError):
        use_case.record_action(**_ids(), status="success", event_id="evt-step08-1")
    # 不同 event_id 不受影响；NULL event_id 可重复。
    use_case.record_action(**_ids(), status="success", event_id="evt-step08-2")
    use_case.record_action(**_ids(), status="denied", event_id=None)
    use_case.record_action(**_ids(), status="denied", event_id=None)


def test_security_and_business_action_rows_are_final_state(audit_case) -> None:
    use_case, engine = audit_case
    audit_id = use_case.record_action(
        **_ids(), status="denied", result_summary={"reason": "out of scope"}
    )
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT status, result_summary FROM agent_audit_log WHERE audit_id=:a"),
            {"a": audit_id},
        ).one()
    assert row.status == "denied"
    assert json.loads(row.result_summary) == {"reason": "out of scope"}
