"""ontology_release 持久化：candidate→validated→active 原子换指针，回滚指向历史版本。

激活唯一由事务内"先退役现役、再按 revision 比较激活"实现；任一步失败保留旧指针。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from application.common.authorization import AuthorizationDenied
from application.common.context import TraceContext
from application.common.errors import ErrorCode
from application.common.events import EventEnvelope, OntologyReleaseActivatedPayload
from infrastructure.messaging.outbox_repository import OutboxRepository


class ReleaseConflict(Exception):
    """revision 比较不匹配或状态不满足：当前激活指针保持不变。"""


class MySqlOntologyReleaseRepository:
    def __init__(
        self,
        engine: Engine,
        outbox: OutboxRepository | None = None,
    ) -> None:
        self._engine = engine
        self._outbox = outbox

    def upsert_candidate(
        self,
        tenant_id: str,
        release_id: str,
        *,
        schema_version: str,
        mapping_version: int,
        metric_version: int,
        manifest: dict,
        digest: str,
        effective_at: datetime | None,
    ) -> None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with self._engine.begin() as connection:
            existing = connection.execute(text(
                "SELECT status FROM ontology_release"
                " WHERE tenant_id=:t AND release_id=:r"
            ), {"t": tenant_id, "r": release_id}).first()
            if existing is not None:
                if existing[0] != "candidate":
                    raise ReleaseConflict("已进入 validated/active 的发布不可覆盖草稿")
                connection.execute(text(
                    "UPDATE ontology_release SET schema_version=:sv,"
                    " mapping_version=:mv, metric_version=:kv,"
                    " manifest_digest=:d, manifest_json=:m, effective_at=:e"
                    " WHERE tenant_id=:t AND release_id=:r"
                ), {"sv": schema_version, "mv": mapping_version, "kv": metric_version,
                    "d": digest, "m": json.dumps(manifest, ensure_ascii=False),
                    "e": effective_at, "t": tenant_id, "r": release_id})
                return
            connection.execute(text(
                "INSERT INTO ontology_release (tenant_id,release_id,schema_version,"
                " mapping_version,metric_version,manifest_digest,manifest_json,status,"
                " revision,effective_at,created_at)"
                " VALUES (:t,:r,:sv,:mv,:kv,:d,:m,'candidate',1,:e,:now)"
            ), {"t": tenant_id, "r": release_id, "sv": schema_version,
                "mv": mapping_version, "kv": metric_version, "d": digest,
                "m": json.dumps(manifest, ensure_ascii=False), "e": effective_at,
                "now": now})

    def mark_validated(self, tenant_id: str, release_id: str) -> None:
        with self._engine.begin() as connection:
            updated = connection.execute(text(
                "UPDATE ontology_release SET status='validated'"
                " WHERE tenant_id=:t AND release_id=:r AND status='candidate'"
            ), {"t": tenant_id, "r": release_id})
            if updated.rowcount != 1:
                raise ReleaseConflict("只有 candidate 可以进入 validated")

    def get(self, tenant_id: str, release_id: str) -> dict[str, Any] | None:
        with self._engine.connect() as connection:
            row = connection.execute(text(
                "SELECT release_id,schema_version,mapping_version,metric_version,"
                " manifest_digest,manifest_json,status,revision,effective_at,"
                " created_at,activated_at FROM ontology_release"
                " WHERE tenant_id=:t AND release_id=:r"
            ), {"t": tenant_id, "r": release_id}).mappings().first()
        return dict(row) if row is not None else None

    def get_active(self, tenant_id: str) -> dict[str, Any] | None:
        with self._engine.connect() as connection:
            row = connection.execute(text(
                "SELECT release_id,schema_version,mapping_version,metric_version,"
                " manifest_digest,manifest_json,status,revision,effective_at,"
                " created_at,activated_at FROM ontology_release"
                " WHERE tenant_id=:t AND status='active'"
            ), {"t": tenant_id}).mappings().first()
        return dict(row) if row is not None else None

    def activate(
        self,
        tenant_id: str,
        release_id: str,
        *,
        expected_active_revision: int | None,
        actor_subject_id: str = "ontology-publisher",
        trace: TraceContext | None = None,
    ) -> dict[str, Any]:
        """原子换指针：现役 revision 与期望匹配才激活；失败抛 ReleaseConflict。"""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with self._engine.begin() as connection:
            active = connection.execute(text(
                "SELECT release_id,revision FROM ontology_release"
                " WHERE tenant_id=:t AND status='active' FOR UPDATE"
            ), {"t": tenant_id}).first()
            current_revision = int(active[1]) if active is not None else 0
            if expected_active_revision is not None and (
                current_revision != expected_active_revision
            ):
                raise ReleaseConflict(
                    f"当前激活 revision={current_revision}，与期望"
                    f" {expected_active_revision} 不符，保留旧版本"
                )
            next_revision = current_revision + 1
            if active is not None and active[0] != release_id:
                connection.execute(text(
                    "UPDATE ontology_release SET status='retired'"
                    " WHERE tenant_id=:t AND release_id=:r AND status='active'"
                ), {"t": tenant_id, "r": active[0]})
            updated = connection.execute(text(
                "UPDATE ontology_release SET status='active', revision=:rev,"
                " activated_at=:now WHERE tenant_id=:t AND release_id=:r"
                " AND status IN ('validated','retired')"
            ), {"rev": next_revision, "now": now, "t": tenant_id, "r": release_id})
            if updated.rowcount != 1:
                raise ReleaseConflict(
                    "目标发布不存在或未通过校验（validated），激活被拒绝"
                )
            row = connection.execute(text(
                "SELECT release_id,schema_version,mapping_version,metric_version,"
                " manifest_digest,manifest_json,status,revision,effective_at,"
                " created_at,activated_at FROM ontology_release"
                " WHERE tenant_id=:t AND release_id=:r"
            ), {"t": tenant_id, "r": release_id}).mappings().first()
            result = dict(row)
            self._emit_activated(connection, tenant_id, result, actor_subject_id, trace)
            return result

    def _emit_activated(
        self,
        connection,
        tenant_id: str,
        release: dict[str, Any],
        actor_subject_id: str,
        trace: TraceContext | None,
    ) -> None:
        if self._outbox is None:
            return
        activated_at = datetime.now(timezone.utc).isoformat()
        request_id = trace.request_id if trace is not None else uuid.uuid4().hex
        correlation_id = trace.correlation_id if trace is not None else uuid.uuid4().hex
        trace_id = trace.trace_id if trace is not None else uuid.uuid4().hex
        thread_id = trace.thread_id if trace is not None else None
        event = EventEnvelope(
            schema_version="1.0",
            event_id=uuid.uuid4().hex,
            event_type="ontology.release.activated",
            event_version=1,
            tenant_id=tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
            trace_id=trace_id,
            operation_id=uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{tenant_id}:ontology_release:{release['release_id']}:activated:"
                f"{release['revision']}",
            ).hex,
            causation_id=None,
            parent_event_id=None,
            producer="ontology-publisher",
            occurred_at=activated_at,
            idempotency_key=(
                f"ontology.release.activated:{release['release_id']}:{release['revision']}"
            ),
            subject_id=actor_subject_id,
            retry_count=0,
            payload=OntologyReleaseActivatedPayload(
                release_id=release["release_id"],
                manifest_digest=release["manifest_digest"],
                mapping_version=str(release["mapping_version"]),
                metric_version=str(release["metric_version"]),
                activated_at=activated_at,
                actor_subject_id=actor_subject_id,
                customer_id=None,
            ),
        )
        self._outbox.enqueue(
            connection,
            event,
            aggregate_type="ontology_release",
            aggregate_id=release["release_id"],
            thread_id=thread_id,
        )

    def list_validated(self, tenant_id: str) -> list[dict[str, Any]]:
        with self._engine.connect() as connection:
            rows = connection.execute(text(
                "SELECT release_id,schema_version,mapping_version,metric_version,"
                " manifest_digest,status,revision,created_at,activated_at"
                " FROM ontology_release WHERE tenant_id=:t"
                " AND status IN ('validated','retired','active')"
                " ORDER BY revision,created_at"
            ), {"t": tenant_id}).mappings().all()
        return [dict(row) for row in rows]


def require_release(value: dict[str, Any] | None, tenant_id: str, release_id: str,
                    ) -> dict[str, Any]:
    if value is None:
        raise AuthorizationDenied(
            ErrorCode.RESOURCE_UNAVAILABLE,
            "资源不存在或不在授权范围内",
        )
    return value
