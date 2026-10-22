"""索引任务的租约、重复通知、过期接管和旧 Worker fencing。"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, text

from infrastructure.persistence.mysql.repositories.knowledge_index_job_repository import (
    MySQLKnowledgeIndexJobRepository,
)


def _engine():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE knowledge_release (tenant_id TEXT,knowledge_release_id TEXT,"
                "manifest_digest TEXT,status TEXT,revision INTEGER,is_active INTEGER,"
                "failure_reason TEXT)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE knowledge_index_job (tenant_id TEXT,knowledge_release_id TEXT,"
                "backend TEXT,manifest_digest TEXT,status TEXT,revision INTEGER,attempts INTEGER,"
                "lease_owner TEXT,lease_until DATETIME,error TEXT,completed_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO knowledge_release VALUES ('t1','r1','digest','draft',0,0,NULL),"
                "('t1','old','old-digest','ready',1,1,NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO knowledge_index_job VALUES "
                "('t1','r1','vector','digest','pending',0,0,NULL,NULL,NULL,NULL)"
            )
        )
    return engine


def test_duplicate_claim_does_not_run_twice_and_completion_is_idempotent() -> None:
    engine = _engine()
    try:
        repo = MySQLKnowledgeIndexJobRepository(engine)
        lease = repo.claim("t1", "r1", "vector", "worker-a")
        assert lease is not None and lease.revision == 1
        assert repo.claim("t1", "r1", "vector", "worker-b") is None
        assert repo.complete(lease, verified_manifest_digest="digest") == "succeeded"
        assert repo.complete(lease, verified_manifest_digest="digest") == "lease_lost"
        assert repo.claim("t1", "r1", "vector", "worker-b") is None
    finally:
        engine.dispose()


def test_expired_lease_fences_old_worker() -> None:
    engine = _engine()
    try:
        repo = MySQLKnowledgeIndexJobRepository(engine)
        assert repo.pending_for_release("r1") == (("t1", "vector"),)
        first = repo.claim("t1", "r1", "vector", "worker-a")
        assert first is not None
        assert repo.pending_for_release("r1") == ()
        expired = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE knowledge_index_job SET lease_until=:expired "
                    "WHERE tenant_id='t1' AND knowledge_release_id='r1'"
                ),
                {"expired": expired},
            )
        assert repo.pending_for_release("r1") == (("t1", "vector"),)
        second = repo.claim("t1", "r1", "vector", "worker-b")
        assert second is not None and second.revision == 2
        assert repo.complete(first, verified_manifest_digest="digest") == "lease_lost"
        assert repo.complete(second, verified_manifest_digest="digest") == "succeeded"
    finally:
        engine.dispose()


def test_failed_verification_preserves_old_release() -> None:
    engine = _engine()
    try:
        repo = MySQLKnowledgeIndexJobRepository(engine)
        lease = repo.claim("t1", "r1", "vector", "worker-a")
        assert lease is not None
        assert repo.complete(lease, verified_manifest_digest="wrong") == "failed"
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT knowledge_release_id,status,is_active FROM knowledge_release "
                    "WHERE tenant_id='t1' ORDER BY knowledge_release_id"
                )
            ).all()
        assert rows == [("old", "ready", 1), ("r1", "indexing_failed", 0)]
    finally:
        engine.dispose()
