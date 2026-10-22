"""步骤21三路索引任务租约与 fencing；完成状态持久化后再尝试发布。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine

from application.knowledge.indexing.models import KnowledgeIndexLease
from application.knowledge.indexing.release_gate import BACKENDS


def _utc_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _as_datetime(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    return datetime.fromisoformat(value)


class MySQLKnowledgeIndexJobRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def pending_for_release(
        self, knowledge_release_id: str, *, limit: int = 12
    ) -> tuple[tuple[str, str], ...]:
        if not knowledge_release_id or limit <= 0:
            raise ValueError("索引任务扫描参数无效")
        with self._engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT j.tenant_id,j.backend FROM knowledge_index_job AS j "
                    "JOIN knowledge_release AS r ON r.tenant_id=j.tenant_id "
                    "AND r.knowledge_release_id=j.knowledge_release_id "
                    "WHERE j.knowledge_release_id=:release_id "
                    "AND r.status IN ('draft','indexing') "
                    "AND (j.status='pending' OR "
                    "(j.status='running' AND j.lease_until<:now)) "
                    "ORDER BY j.tenant_id,j.backend LIMIT :limit"
                ),
                {"release_id": knowledge_release_id, "now": _utc_naive(), "limit": limit},
            ).all()
        return tuple((str(row[0]), str(row[1])) for row in rows)

    def claim(
        self,
        tenant_id: str,
        knowledge_release_id: str,
        backend: str,
        worker_id: str,
        *,
        lease_seconds: int = 60,
    ) -> KnowledgeIndexLease | None:
        if not tenant_id or not knowledge_release_id or not worker_id:
            raise ValueError("索引任务身份不能为空")
        if backend not in BACKENDS or lease_seconds <= 0:
            raise ValueError("索引后端或租约无效")
        now = _utc_naive()
        lock = " FOR UPDATE" if self._engine.dialect.name == "mysql" else ""
        with self._engine.begin() as connection:
            release = (
                connection.execute(
                    text(
                        "SELECT status,manifest_digest FROM knowledge_release "
                        "WHERE tenant_id=:tenant_id AND knowledge_release_id=:release_id" + lock
                    ),
                    {"tenant_id": tenant_id, "release_id": knowledge_release_id},
                )
                .mappings()
                .first()
            )
            if release is None or release["status"] not in ("draft", "indexing"):
                return None
            row = (
                connection.execute(
                    text(
                        "SELECT status,revision,lease_until,manifest_digest "
                        "FROM knowledge_index_job WHERE tenant_id=:tenant_id "
                        "AND knowledge_release_id=:release_id AND backend=:backend" + lock
                    ),
                    {
                        "tenant_id": tenant_id,
                        "release_id": knowledge_release_id,
                        "backend": backend,
                    },
                )
                .mappings()
                .first()
            )
            if row is None or row["status"] in ("succeeded", "failed"):
                return None
            if row["manifest_digest"] != release["manifest_digest"]:
                raise ValueError("索引任务与发布清单摘要不一致")
            until = _as_datetime(row["lease_until"])
            if row["status"] == "running" and until is not None and until > now:
                return None
            revision = int(row["revision"]) + 1
            result = connection.execute(
                text(
                    "UPDATE knowledge_index_job SET status='running',revision=:revision, "
                    "attempts=attempts+1,lease_owner=:worker_id,lease_until=:lease_until "
                    "WHERE tenant_id=:tenant_id AND knowledge_release_id=:release_id "
                    "AND backend=:backend AND revision=:old_revision"
                ),
                {
                    "revision": revision,
                    "worker_id": worker_id,
                    "lease_until": now + timedelta(seconds=lease_seconds),
                    "tenant_id": tenant_id,
                    "release_id": knowledge_release_id,
                    "backend": backend,
                    "old_revision": row["revision"],
                },
            )
            if result.rowcount != 1:
                raise RuntimeError("索引任务租约条件更新失败")
            connection.execute(
                text(
                    "UPDATE knowledge_release SET status='indexing',revision=revision+1 "
                    "WHERE tenant_id=:tenant_id AND knowledge_release_id=:release_id "
                    "AND status='draft'"
                ),
                {"tenant_id": tenant_id, "release_id": knowledge_release_id},
            )
            return KnowledgeIndexLease(
                tenant_id,
                knowledge_release_id,
                backend,
                str(row["manifest_digest"]),
                worker_id,
                revision,
            )

    def complete(
        self,
        lease: KnowledgeIndexLease,
        *,
        verified_manifest_digest: str | None,
    ) -> str:
        """只接受当前租约；摘要未核实则把候选标为失败，不触碰旧版本。"""
        now = _utc_naive()
        success = verified_manifest_digest == lease.manifest_digest
        status = "succeeded" if success else "failed"
        with self._engine.begin() as connection:
            result = connection.execute(
                text(
                    "UPDATE knowledge_index_job SET status=:status,revision=revision+1, "
                    "lease_owner=NULL,lease_until=NULL,completed_at=:now, "
                    "error=:error WHERE tenant_id=:tenant_id "
                    "AND knowledge_release_id=:release_id AND backend=:backend "
                    "AND manifest_digest=:digest AND status='running' "
                    "AND lease_owner=:worker_id AND lease_until>=:now "
                    "AND revision=:revision"
                ),
                {
                    "status": status,
                    "now": now,
                    "error": None if success else "manifest_verification_failed",
                    "tenant_id": lease.tenant_id,
                    "release_id": lease.knowledge_release_id,
                    "backend": lease.backend,
                    "digest": lease.manifest_digest,
                    "worker_id": lease.worker_id,
                    "revision": lease.revision,
                },
            )
            if result.rowcount != 1:
                return "lease_lost"
            if not success:
                connection.execute(
                    text(
                        "UPDATE knowledge_release SET status='indexing_failed', "
                        "failure_reason='manifest_verification_failed',revision=revision+1 "
                        "WHERE tenant_id=:tenant_id AND knowledge_release_id=:release_id "
                        "AND is_active=0"
                    ),
                    {"tenant_id": lease.tenant_id, "release_id": lease.knowledge_release_id},
                )
            return status
