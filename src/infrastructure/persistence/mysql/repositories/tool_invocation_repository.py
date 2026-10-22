"""MySQL 工具调用记录：跨恢复保留尝试预算并隔离旧租约。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine

from application.common.execution.retry_policy import TRANSIENT_ERROR_CODES
from application.common.masking import mask_mapping
from application.common.tasks.models import (
    TaskLeaseLostError,
    ToolInvocationConflictError,
    ToolInvocationLease,
)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class MySQLToolInvocationRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def result_for(
        self, tenant_id: str, request_id: str, tool_name: str, digest: str
    ) -> tuple[str, dict | None] | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    text("""SELECT status,result_summary FROM tool_invocation
                    WHERE tenant_id=:tenant_id AND request_id=:request_id
                    AND tool_name=:tool_name AND normalized_args_digest=:digest"""),
                    {
                        "tenant_id": tenant_id,
                        "request_id": request_id,
                        "tool_name": tool_name,
                        "digest": digest,
                    },
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        summary = row["result_summary"]
        return row["status"], json.loads(summary) if isinstance(summary, str) else summary

    def reserve(
        self,
        *,
        tenant_id: str,
        request_id: str,
        agent_id: str,
        tool_name: str,
        normalized_args_digest: str,
        args_summary: dict,
        access_mode: str,
        worker_id: str,
        lease_s: int = 30,
    ) -> ToolInvocationLease | None:
        if access_mode not in ("read", "write") or len(normalized_args_digest) != 64:
            raise ValueError("调用访问模式或参数摘要无效")
        now = _now()
        masked = json.dumps(mask_mapping(args_summary), ensure_ascii=False, sort_keys=True)
        with self._engine.begin() as connection:
            connection.execute(
                text("""INSERT INTO tool_invocation (
                    tenant_id,invocation_id,request_id,agent_id,tool_name,
                    normalized_args_digest,args_summary,access_mode,attempt_count,
                    retry_count,status,result_summary,error,revision,lease_owner,
                    lease_until,fencing_token,created_at,updated_at
                ) VALUES (
                    :tenant_id,:invocation_id,:request_id,:agent_id,:tool_name,
                    :digest,:summary,:access_mode,0,0,'pending',NULL,NULL,0,NULL,
                    NULL,0,:now,:now
                ) ON DUPLICATE KEY UPDATE invocation_id=invocation_id"""),
                {
                    "tenant_id": tenant_id,
                    "invocation_id": f"inv-{uuid.uuid4().hex}",
                    "request_id": request_id,
                    "agent_id": agent_id,
                    "tool_name": tool_name,
                    "digest": normalized_args_digest,
                    "summary": masked,
                    "access_mode": access_mode,
                    "now": now,
                },
            )
            row = (
                connection.execute(
                    text("""SELECT * FROM tool_invocation WHERE tenant_id=:tenant_id
                    AND request_id=:request_id AND tool_name=:tool_name
                    AND normalized_args_digest=:digest FOR UPDATE"""),
                    {
                        "tenant_id": tenant_id,
                        "request_id": request_id,
                        "tool_name": tool_name,
                        "digest": normalized_args_digest,
                    },
                )
                .mappings()
                .one()
            )
            if row["agent_id"] != agent_id or row["access_mode"] != access_mode:
                raise ToolInvocationConflictError("同一工具调用键的身份或访问模式不一致")
            if row["status"] in ("succeeded", "unknown"):
                return None
            if row["status"] == "running" and row["lease_until"] > now:
                return None
            if row["status"] == "failed" and row["error"] not in TRANSIENT_ERROR_CODES:
                return None
            attempts = int(row["attempt_count"])
            if attempts > 0 and access_mode == "write":
                connection.execute(
                    text("""UPDATE tool_invocation SET status='unknown',revision=revision+1,
                    lease_owner=NULL,lease_until=NULL,updated_at=:now
                    WHERE tenant_id=:tenant_id AND invocation_id=:invocation_id"""),
                    {"now": now, "tenant_id": tenant_id, "invocation_id": row["invocation_id"]},
                )
                return None
            if attempts >= 2:
                return None
            token = int(row["fencing_token"]) + 1
            connection.execute(
                text("""UPDATE tool_invocation SET status='running',
                attempt_count=:attempts,retry_count=:retries,lease_owner=:worker_id,
                lease_until=:lease_until,fencing_token=:token,revision=revision+1,
                updated_at=:now WHERE tenant_id=:tenant_id AND invocation_id=:invocation_id"""),
                {
                    "attempts": attempts + 1,
                    "retries": attempts,
                    "worker_id": worker_id,
                    "lease_until": now + timedelta(seconds=lease_s),
                    "token": token,
                    "now": now,
                    "tenant_id": tenant_id,
                    "invocation_id": row["invocation_id"],
                },
            )
            return ToolInvocationLease(
                row["invocation_id"],
                tenant_id,
                request_id,
                tool_name,
                attempts + 1,
                attempts,
                worker_id,
                token,
            )

    def finish(
        self,
        lease: ToolInvocationLease,
        *,
        status: str,
        result_summary: dict | None = None,
        error: str | None = None,
    ) -> None:
        if status not in ("succeeded", "failed", "unknown"):
            raise ValueError("工具调用终态无效")
        now = _now()
        summary = (
            json.dumps(mask_mapping(result_summary), ensure_ascii=False, sort_keys=True)
            if result_summary is not None
            else None
        )
        with self._engine.begin() as connection:
            result = connection.execute(
                text("""UPDATE tool_invocation SET status=:status,result_summary=:summary,
                error=:error,revision=revision+1,lease_owner=NULL,lease_until=NULL,
                updated_at=:now WHERE tenant_id=:tenant_id AND invocation_id=:invocation_id
                AND status='running' AND lease_owner=:worker_id AND fencing_token=:token
                AND lease_until>=:now"""),
                {
                    "status": status,
                    "summary": summary,
                    "error": error[:128] if error else None,
                    "now": now,
                    "tenant_id": lease.tenant_id,
                    "invocation_id": lease.invocation_id,
                    "worker_id": lease.worker_id,
                    "token": lease.fencing_token,
                },
            )
            if result.rowcount != 1:
                raise TaskLeaseLostError("工具调用租约已失效，旧执行者不得提交")
