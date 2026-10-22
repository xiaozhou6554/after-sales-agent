"""MySQL 任务原子创建、租约领取、fencing 与同事务完成。"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from application.common.events import EventEnvelope
from application.common.tasks.models import (
    TaskConflictError,
    TaskLease,
    TaskLeaseLostError,
    TaskRecord,
)
from infrastructure.messaging.outbox_repository import OutboxRepository


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _record(row) -> TaskRecord:
    payload = row["input_masked"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return TaskRecord(
        tenant_id=row["tenant_id"],
        task_id=row["task_id"],
        customer_id=row["customer_id"],
        actor_subject_id=row["actor_subject_id"],
        thread_id=row["thread_id"],
        task_type=row["task_type"],
        input_masked=payload,
        input_digest=row["input_digest"],
        status=row["status"],
        revision=int(row["revision"]),
        checkpoint_ref=row["checkpoint_ref"],
        result_ref=row["result_ref"],
        error=row["error"],
        idempotency_key=row["idempotency_key"],
        deadline_at=row["deadline_at"].replace(tzinfo=timezone.utc),
        updated_at=row["updated_at"].replace(tzinfo=timezone.utc),
    )


class MySQLTaskRepository:
    def __init__(self, engine: Engine, outbox: OutboxRepository) -> None:
        self._engine = engine
        self._outbox = outbox

    def create(
        self,
        *,
        tenant_id: str,
        task_id: str,
        customer_id: str,
        actor_subject_id: str,
        thread_id: str,
        task_type: str,
        input_masked: dict,
        input_digest: str,
        checkpoint_ref: str | None,
        idempotency_key: str,
        deadline_at: datetime,
        event: EventEnvelope,
    ) -> TaskRecord:
        now = _now()
        with self._engine.begin() as connection:
            connection.execute(
                text("""INSERT INTO agent_task (
                    tenant_id,task_id,customer_id,actor_subject_id,thread_id,task_type,
                    input_masked,input_digest,status,revision,checkpoint_ref,result_ref,
                    error,idempotency_key,lease_owner,lease_until,fencing_token,deadline_at,
                    created_at,updated_at
                ) VALUES (
                    :tenant_id,:task_id,:customer_id,:actor_subject_id,:thread_id,:task_type,
                    :input_masked,:input_digest,'pending',0,:checkpoint_ref,NULL,
                    NULL,:idempotency_key,NULL,NULL,0,:deadline_at,:now,:now
                ) ON DUPLICATE KEY UPDATE task_id=task_id"""),
                {
                    "tenant_id": tenant_id,
                    "task_id": task_id,
                    "customer_id": customer_id,
                    "actor_subject_id": actor_subject_id,
                    "thread_id": thread_id,
                    "task_type": task_type,
                    "input_masked": json.dumps(input_masked, ensure_ascii=False, sort_keys=True),
                    "input_digest": input_digest,
                    "checkpoint_ref": checkpoint_ref,
                    "idempotency_key": idempotency_key,
                    "deadline_at": deadline_at.astimezone(timezone.utc).replace(tzinfo=None),
                    "now": now,
                },
            )
            row = (
                connection.execute(
                    text("""SELECT * FROM agent_task WHERE tenant_id=:tenant_id
                    AND actor_subject_id=:actor_subject_id AND task_type=:task_type
                    AND idempotency_key=:idempotency_key FOR UPDATE"""),
                    {
                        "tenant_id": tenant_id,
                        "actor_subject_id": actor_subject_id,
                        "task_type": task_type,
                        "idempotency_key": idempotency_key,
                    },
                )
                .mappings()
                .one()
            )
            if row["input_digest"] != input_digest:
                raise TaskConflictError("同一任务幂等键对应不同请求内容")
            if row["task_id"] == task_id:
                self._outbox.enqueue(
                    connection,
                    event,
                    aggregate_type="agent_task",
                    aggregate_id=task_id,
                    thread_id=thread_id,
                )
            return _record(row)

    def get(self, tenant_id: str, task_id: str) -> TaskRecord | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT * FROM agent_task WHERE tenant_id=:tenant_id AND task_id=:task_id"
                    ),
                    {"tenant_id": tenant_id, "task_id": task_id},
                )
                .mappings()
                .first()
            )
        return _record(row) if row is not None else None

    def get_by_idempotency(
        self, tenant_id: str, actor_subject_id: str, task_type: str, idempotency_key: str
    ) -> TaskRecord | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    text("""SELECT * FROM agent_task WHERE tenant_id=:tenant_id
                    AND actor_subject_id=:actor_subject_id AND task_type=:task_type
                    AND idempotency_key=:idempotency_key"""),
                    {
                        "tenant_id": tenant_id,
                        "actor_subject_id": actor_subject_id,
                        "task_type": task_type,
                        "idempotency_key": idempotency_key,
                    },
                )
                .mappings()
                .first()
            )
        return _record(row) if row is not None else None

    def pending_tenants(self, task_type: str) -> list[str]:
        now = _now()
        with self._engine.connect() as connection:
            rows = connection.execute(
                text("""SELECT DISTINCT tenant_id FROM agent_task WHERE task_type=:task_type
                    AND (status='pending' OR (status='running' AND lease_until<:now))"""),
                {"task_type": task_type, "now": now},
            )
            return [str(row[0]) for row in rows]

    def claim(
        self, tenant_id: str, task_type: str, worker_id: str, *, lease_s: int = 60
    ) -> TaskLease | None:
        now = _now()
        with self._engine.begin() as connection:
            row = (
                connection.execute(
                    text("""SELECT * FROM agent_task WHERE tenant_id=:tenant_id
                    AND task_type=:task_type AND
                    (status='pending' OR (status='running' AND lease_until<:now))
                    ORDER BY created_at,task_id LIMIT 1 FOR UPDATE SKIP LOCKED"""),
                    {"tenant_id": tenant_id, "task_type": task_type, "now": now},
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            if row["deadline_at"] <= now:
                connection.execute(
                    text("""UPDATE agent_task SET status='failed',error='TASK_DEADLINE_EXCEEDED',
                        lease_owner=NULL,lease_until=NULL,revision=revision+1,updated_at=:now
                        WHERE tenant_id=:tenant_id AND task_id=:task_id"""),
                    {"tenant_id": tenant_id, "task_id": row["task_id"], "now": now},
                )
                return None
            token = int(row["fencing_token"]) + 1
            connection.execute(
                text("""UPDATE agent_task SET status='running',lease_owner=:worker_id,
                    lease_until=:lease_until,fencing_token=:token,revision=revision+1,
                    updated_at=:now WHERE tenant_id=:tenant_id AND task_id=:task_id"""),
                {
                    "worker_id": worker_id,
                    "lease_until": now + timedelta(seconds=lease_s),
                    "token": token,
                    "now": now,
                    "tenant_id": tenant_id,
                    "task_id": row["task_id"],
                },
            )
            claimed = (
                connection.execute(
                    text(
                        "SELECT * FROM agent_task WHERE tenant_id=:tenant_id AND task_id=:task_id"
                    ),
                    {"tenant_id": tenant_id, "task_id": row["task_id"]},
                )
                .mappings()
                .one()
            )
            return TaskLease(_record(claimed), worker_id, token)

    def complete(
        self,
        lease: TaskLease,
        result_ref: str,
        effect: Callable[[Connection, TaskRecord], None] | None = None,
    ) -> TaskRecord:
        return self._finish(lease, "succeeded", result_ref, None, effect)

    def fail(self, lease: TaskLease, error: str) -> TaskRecord:
        return self._finish(lease, "failed", None, error[:512], None)

    def _finish(
        self,
        lease: TaskLease,
        status: str,
        result_ref: str | None,
        error: str | None,
        effect: Callable[[Connection, TaskRecord], None] | None,
    ) -> TaskRecord:
        now = _now()
        task = lease.task
        with self._engine.begin() as connection:
            row = (
                connection.execute(
                    text("""SELECT * FROM agent_task WHERE tenant_id=:tenant_id
                    AND task_id=:task_id FOR UPDATE"""),
                    {"tenant_id": task.tenant_id, "task_id": task.task_id},
                )
                .mappings()
                .one()
            )
            if (
                row["status"] != "running"
                or row["lease_owner"] != lease.worker_id
                or int(row["fencing_token"]) != lease.fencing_token
                or row["lease_until"] < now
            ):
                raise TaskLeaseLostError("任务租约已失效，旧 Worker 不得提交")
            if effect is not None:
                effect(connection, _record(row))
            connection.execute(
                text("""UPDATE agent_task SET status=:status,result_ref=:result_ref,error=:error,
                    lease_owner=NULL,lease_until=NULL,revision=revision+1,updated_at=:now
                    WHERE tenant_id=:tenant_id AND task_id=:task_id"""),
                {
                    "status": status,
                    "result_ref": result_ref,
                    "error": error,
                    "now": now,
                    "tenant_id": task.tenant_id,
                    "task_id": task.task_id,
                },
            )
            finished = (
                connection.execute(
                    text(
                        "SELECT * FROM agent_task WHERE tenant_id=:tenant_id AND task_id=:task_id"
                    ),
                    {"tenant_id": task.tenant_id, "task_id": task.task_id},
                )
                .mappings()
                .one()
            )
            return _record(finished)
