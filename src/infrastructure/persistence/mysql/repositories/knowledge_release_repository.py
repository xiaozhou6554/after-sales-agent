"""只读查询已激活且三路索引均成功的知识版本。"""

from __future__ import annotations

import hashlib

from sqlalchemy import text
from sqlalchemy.engine import Engine

from application.knowledge.indexing import (
    IndexJobState,
    PublicationDecision,
    decide_publication,
)


class MySQLActiveKnowledgeReleaseRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get_active_release_id(self, tenant_id: str) -> str | None:
        if not tenant_id:
            raise ValueError("可信租户不能为空")
        with self._engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT r.knowledge_release_id,r.manifest_digest,m.manifest_json "
                    "FROM knowledge_release AS r "
                    "JOIN knowledge_manifest AS m ON m.tenant_id=r.tenant_id "
                    "AND m.knowledge_release_id=r.knowledge_release_id "
                    "AND m.manifest_digest=r.manifest_digest "
                    "WHERE r.tenant_id=:tenant_id AND r.status='ready' AND r.is_active=1 "
                    "AND (SELECT COUNT(*) FROM knowledge_index_job AS j "
                    "WHERE j.tenant_id=r.tenant_id "
                    "AND j.knowledge_release_id=r.knowledge_release_id "
                    "AND j.manifest_digest=r.manifest_digest "
                    "AND j.status='succeeded' "
                    "AND j.backend IN ('vector','bm25','graph'))=3 LIMIT 2"
                ),
                {"tenant_id": tenant_id},
            ).fetchall()
        if len(rows) > 1:
            raise RuntimeError("同一租户存在多个已激活知识版本")
        if not rows:
            return None
        if hashlib.sha256(str(rows[0][2]).encode("utf-8")).hexdigest() != rows[0][1]:
            return None
        return str(rows[0][0])


class MySQLKnowledgeReleasePublisher:
    """锁定租户发布行，在同一事务内判定三路任务并切换可见版本。"""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get_revision(self, tenant_id: str, knowledge_release_id: str) -> int | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT revision FROM knowledge_release WHERE tenant_id=:tenant_id "
                    "AND knowledge_release_id=:release_id"
                ),
                {"tenant_id": tenant_id, "release_id": knowledge_release_id},
            ).first()
        return int(row[0]) if row is not None else None

    def activate_if_complete(
        self, tenant_id: str, knowledge_release_id: str, expected_revision: int
    ) -> str:
        if not tenant_id or not knowledge_release_id or expected_revision < 0:
            raise ValueError("发布参数无效")
        lock = " FOR UPDATE" if self._engine.dialect.name == "mysql" else ""
        with self._engine.begin() as connection:
            # 按主键顺序锁定该租户所有版本；并发激活不能各自读到旧的 active 状态。
            releases = (
                connection.execute(
                    text(
                        "SELECT knowledge_release_id,manifest_digest,status,revision,is_active "
                        "FROM knowledge_release WHERE tenant_id=:tenant_id "
                        "ORDER BY knowledge_release_id" + lock
                    ),
                    {"tenant_id": tenant_id},
                )
                .mappings()
                .all()
            )
            target = next(
                (row for row in releases if row["knowledge_release_id"] == knowledge_release_id),
                None,
            )
            if target is None:
                return "not_found"
            if target["is_active"] and target["status"] == "ready":
                return "already_active"
            if target["status"] == "indexing_failed":
                return "indexing_failed"
            if target["revision"] != expected_revision:
                return "revision_conflict"
            snapshot = connection.execute(
                text(
                    "SELECT manifest_json,manifest_digest FROM knowledge_manifest "
                    "WHERE tenant_id=:tenant_id AND knowledge_release_id=:release_id" + lock
                ),
                {"tenant_id": tenant_id, "release_id": knowledge_release_id},
            ).first()
            snapshot_valid = bool(
                snapshot is not None
                and snapshot[1] == target["manifest_digest"]
                and hashlib.sha256(str(snapshot[0]).encode("utf-8")).hexdigest()
                == target["manifest_digest"]
            )
            jobs = tuple(
                IndexJobState(
                    str(row["backend"]),
                    knowledge_release_id,
                    str(row["manifest_digest"]),
                    str(row["status"]),
                    int(row["revision"]),
                )
                for row in connection.execute(
                    text(
                        "SELECT backend,manifest_digest,status,revision "
                        "FROM knowledge_index_job WHERE tenant_id=:tenant_id "
                        "AND knowledge_release_id=:release_id ORDER BY backend" + lock
                    ),
                    {"tenant_id": tenant_id, "release_id": knowledge_release_id},
                ).mappings()
            )
            decision = (
                decide_publication(knowledge_release_id, str(target["manifest_digest"]), jobs)
                if snapshot_valid
                else PublicationDecision("indexing_failed", "持久化清单摘要不一致")
            )
            if decision.status == "pending":
                return "pending"
            if decision.status == "indexing_failed":
                connection.execute(
                    text(
                        "UPDATE knowledge_release SET status='indexing_failed', "
                        "failure_reason=:reason, revision=revision+1 "
                        "WHERE tenant_id=:tenant_id AND knowledge_release_id=:release_id "
                        "AND revision=:revision AND is_active=0"
                    ),
                    {
                        "reason": decision.reason,
                        "tenant_id": tenant_id,
                        "release_id": knowledge_release_id,
                        "revision": expected_revision,
                    },
                )
                return "indexing_failed"
            connection.execute(
                text(
                    "UPDATE knowledge_release SET is_active=0 WHERE tenant_id=:tenant_id "
                    "AND is_active=1 AND knowledge_release_id<>:release_id"
                ),
                {"tenant_id": tenant_id, "release_id": knowledge_release_id},
            )
            result = connection.execute(
                text(
                    "UPDATE knowledge_release SET status='ready',is_active=1, "
                    "published_at=CURRENT_TIMESTAMP,revision=revision+1,failure_reason=NULL "
                    "WHERE tenant_id=:tenant_id AND knowledge_release_id=:release_id "
                    "AND revision=:revision AND is_active=0"
                ),
                {
                    "tenant_id": tenant_id,
                    "release_id": knowledge_release_id,
                    "revision": expected_revision,
                },
            )
            if result.rowcount != 1:
                raise RuntimeError("发布条件更新失败，事务已回滚")
            return "activated"
