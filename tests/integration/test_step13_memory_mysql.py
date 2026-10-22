"""步骤13真实 MySQL 画像持久化与租户/客户隔离。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from bootstrap.migrate import build_executor, run_apply
from bootstrap.settings import load_settings
from sqlalchemy import text

from application.memory.use_cases.maintenance import MemoryMaintenanceUseCase
from domain.memory.entities.memory_fact import MemoryFact
from infrastructure.persistence.mysql.connection import create_mysql_engine
from infrastructure.persistence.mysql.repositories.memory_profile_repository import (
    MySQLMemoryProfileRepository,
)


@pytest.fixture()
def profile_case():
    settings = load_settings()
    run_apply(build_executor(settings))
    engine = create_mysql_engine(
        settings.mysql_url, settings.mysql_connect_timeout_s, settings.mysql_read_timeout_s
    )
    repository = MySQLMemoryProfileRepository(engine)
    yield repository, engine
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM user_memory_fact WHERE tenant_id='tenant-step13'"))
        connection.execute(text("DELETE FROM user_profile WHERE tenant_id='tenant-step13'"))
    engine.dispose()


def test_profile_survives_repository_recreation_and_is_scoped(profile_case) -> None:
    repository, engine = profile_case
    fact = MemoryFact(
        "product_preference", "颜色", "黑色", 0.85, "conversation", datetime.now(timezone.utc)
    )
    repository.upsert("tenant-step13", "customer-a", fact)
    restored = MySQLMemoryProfileRepository(engine)
    assert restored.load("tenant-step13", "customer-a").facts[0].fact_value == "黑色"
    assert restored.load("tenant-step13", "customer-b").facts == []
    assert restored.load("tenant-other", "customer-a").facts == []
    repository.upsert("tenant-step13", "customer-a", fact)
    assert len(restored.load("tenant-step13", "customer-a").facts) == 1


def test_decay_cleanup_deletes_ordinary_fact_but_keeps_important_fact(profile_case) -> None:
    repository, engine = profile_case
    now = datetime.now(timezone.utc)
    repository.upsert(
        "tenant-step13",
        "customer-a",
        MemoryFact("product_preference", "普通颜色", "黑色", 0.5, "conversation", now),
    )
    repository.upsert(
        "tenant-step13",
        "customer-a",
        MemoryFact("product_preference", "重要型号", "型号A", 0.8, "conversation", now),
    )
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE user_memory_fact SET updated_at=:old WHERE tenant_id=:tenant_id"),
            {"old": (now - timedelta(days=180)).replace(tzinfo=None), "tenant_id": "tenant-step13"},
        )
    deleted = MemoryMaintenanceUseCase(repository).run(now, tenant_id="tenant-step13")
    assert deleted == 1
    assert [fact.fact_key for fact in repository.load("tenant-step13", "customer-a").facts] == [
        "重要型号"
    ]
