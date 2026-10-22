"""步骤15：真实 MySQL 工具调用预算、未知结果与旧租约隔离。"""

from __future__ import annotations

import hashlib
import json
import uuid

import pytest
from bootstrap.settings import load_settings
from sqlalchemy import text

from application.common.tasks.models import TaskLeaseLostError, ToolInvocationConflictError
from infrastructure.persistence.mysql.connection import create_mysql_engine
from infrastructure.persistence.mysql.repositories.tool_invocation_repository import (
    MySQLToolInvocationRepository,
)


@pytest.fixture()
def invocation_case():
    settings = load_settings()
    engine = create_mysql_engine(
        settings.mysql_url, settings.mysql_connect_timeout_s, settings.mysql_read_timeout_s
    )
    tenant = f"step15-inv-{uuid.uuid4().hex[:12]}"
    try:
        yield tenant, engine, MySQLToolInvocationRepository(engine)
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM tool_invocation WHERE tenant_id=:tenant"), {"tenant": tenant}
            )
        engine.dispose()


def _reserve(repository, tenant, *, access="read", worker="worker-a", lease_s=30):
    return repository.reserve(
        tenant_id=tenant,
        request_id="req-step15-inv",
        agent_id="supervisor",
        tool_name="query_order",
        normalized_args_digest=hashlib.sha256(b"order-001").hexdigest(),
        args_summary={"order_id": "order-001", "phone": "13812345678"},
        access_mode=access,
        worker_id=worker,
        lease_s=lease_s,
    )


def test_read_only_budget_survives_repository_recreation(invocation_case) -> None:
    tenant, engine, repository = invocation_case
    first = _reserve(repository, tenant, lease_s=0)
    assert first is not None
    assert _reserve(repository, tenant, worker="worker-overlap") is not None
    restarted = MySQLToolInvocationRepository(engine)
    second = _reserve(restarted, tenant, worker="worker-b")
    assert second is None  # 第二次尝试已有执行者占用，不能并发第三次。
    with pytest.raises(TaskLeaseLostError):
        repository.finish(first, status="succeeded")
    with engine.connect() as connection:
        row = (
            connection.execute(
                text("""SELECT attempt_count,retry_count,status,args_summary FROM tool_invocation
                WHERE tenant_id=:tenant"""),
                {"tenant": tenant},
            )
            .mappings()
            .one()
        )
    assert row["attempt_count"] == 2
    assert row["retry_count"] == 1
    assert row["status"] == "running"
    summary = row["args_summary"]
    if isinstance(summary, str):
        summary = json.loads(summary)
    assert "13812345678" not in json.dumps(summary)


def test_write_timeout_becomes_unknown_without_reexecution(invocation_case) -> None:
    tenant, engine, repository = invocation_case
    first = _reserve(repository, tenant, access="write", lease_s=0)
    assert first is not None
    assert _reserve(repository, tenant, access="write", worker="worker-b") is None
    with pytest.raises(TaskLeaseLostError):
        repository.finish(first, status="succeeded")
    with engine.connect() as connection:
        row = (
            connection.execute(
                text("""SELECT status,attempt_count,retry_count FROM tool_invocation
                WHERE tenant_id=:tenant"""),
                {"tenant": tenant},
            )
            .mappings()
            .one()
        )
    assert (row["status"], row["attempt_count"], row["retry_count"]) == ("unknown", 1, 0)
    assert _reserve(repository, tenant, access="write", worker="worker-c") is None


def test_identity_conflict_and_nontransient_failure_block_retry(invocation_case) -> None:
    tenant, _engine, repository = invocation_case
    first = _reserve(repository, tenant)
    assert first is not None
    with pytest.raises(ToolInvocationConflictError):
        _reserve(repository, tenant, access="write")
    repository.finish(first, status="failed", error="INVALID_ARGUMENT")
    assert _reserve(repository, tenant, worker="worker-b") is None
