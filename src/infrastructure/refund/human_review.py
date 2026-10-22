"""开发环境受授权人工审核；拒绝时与申请状态同事务释放占用。"""

from __future__ import annotations

import json
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Connection

from application.common.authorization import (
    GENERIC_DENIED_MESSAGE,
    AuthorizationDenied,
    require_customer_access,
    verify_tool_context,
)
from application.common.context import AuthContext, TraceContext
from application.common.errors import ErrorCode
from application.common.events import EventEnvelope, RefundApprovedPayload
from application.common.operation_confirmation import content_digest
from domain.refund.review import RefundReview, decide_review
from infrastructure.messaging.idempotency_store import MySQLIdempotencyStore
from infrastructure.messaging.outbox_repository import OutboxRepository
from infrastructure.mock.refund_snapshot_adapter import MockRefundSnapshotAdapter
from infrastructure.refund.submission_adapter import RefundSubmissionConflict


class MySqlMockRefundHumanReview:
    def __init__(
        self,
        engine,
        snapshots: MockRefundSnapshotAdapter,
        outbox: OutboxRepository | None = None,
        connection: Connection | None = None,
    ) -> None:
        self._engine = engine
        self._snapshots = snapshots
        self._outbox = outbox
        self._connection = connection

    def bind(self, connection: Connection) -> "MySqlMockRefundHumanReview":
        return MySqlMockRefundHumanReview(self._engine, self._snapshots, self._outbox, connection)

    @contextmanager
    def _transaction(self):
        if self._connection is not None:
            with self._connection.begin_nested():
                yield self._connection
        else:
            with self._engine.begin() as connection:
                yield connection

    def decide(
        self,
        auth: AuthContext,
        refund_request_id: str,
        *,
        expected_revision: int,
        approve: bool,
        reason_code: str,
        idempotency_key: str,
        trace: TraceContext,
    ) -> RefundReview:
        verify_tool_context(auth, ("refund:review",))
        if not set(auth.roles) & {"support", "human_agent", "supervisor", "admin"}:
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
        if not refund_request_id:
            raise ValueError("退款申请标识不能为空")
        if (
            not isinstance(reason_code, str)
            or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", reason_code) is None
        ):
            raise ValueError("退款审核原因必须是受控代码")
        if not idempotency_key or len(idempotency_key) > 64:
            raise ValueError("退款审核幂等键无效")
        if not isinstance(trace, TraceContext) or not all(
            (trace.request_id, trace.correlation_id, trace.trace_id)
        ):
            raise ValueError("退款审核缺少可信链路标识")
        if approve and self._outbox is None:
            raise RefundSubmissionConflict("退款审核批准事件尚未装配，不得批准")
        digest = content_digest(
            {
                "refund_request_id": refund_request_id,
                "expected_revision": expected_revision,
                "approve": approve,
                "reason_code": reason_code,
            }
        )
        with self._transaction() as conn:
            row = conn.execute(
                text(
                    "SELECT order_id,customer_id,review_status,review_revision,eligibility,"
                    "goods_minor,shipping_minor,amount_minor,currency,actor_subject_id,"
                    "reviewer_subject_id,review_reason_code"
                    " FROM refund_request"
                    " WHERE tenant_id=:tenant_id AND refund_request_id=:refund_request_id"
                    " FOR UPDATE"
                ),
                {"tenant_id": auth.tenant_id, "refund_request_id": refund_request_id},
            ).first()
            if row is None:
                raise RefundSubmissionConflict("退款申请不存在或不在授权范围内")
            require_customer_access(auth, row.customer_id, tenant_id=auth.tenant_id)
            snapshot = self._snapshots.bind(conn).load(auth.tenant_id, row.order_id)
            if snapshot is None or snapshot.customer_id != row.customer_id:
                raise RefundSubmissionConflict("退款审核订单来源不可用")
            idempotency = MySQLIdempotencyStore(self._engine).bind(conn)
            idem = idempotency.reserve(
                auth.tenant_id,
                auth.subject_id,
                "refund_review",
                idempotency_key,
                digest,
            )
            if idem.status == "succeeded":
                if (
                    idem.result_ref != refund_request_id
                    or row.review_status != ("approved" if approve else "rejected")
                    or row.reviewer_subject_id != auth.subject_id
                    or row.review_reason_code != reason_code
                ):
                    raise RefundSubmissionConflict("原退款审核结果已变化，需查询状态")
                return RefundReview(
                    refund_request_id,
                    row.review_status,
                    int(row.review_revision),
                    row.eligibility,
                    snapshot.facts.fulfillment_status,
                    row.reviewer_subject_id,
                    row.review_reason_code,
                )
            if idem.status != "pending":
                raise RefundSubmissionConflict("原退款审核结果待核实")
            review = RefundReview(
                refund_request_id,
                row.review_status,
                int(row.review_revision),
                row.eligibility,
                snapshot.facts.fulfillment_status,
            )
            changed = decide_review(
                review,
                expected_revision=expected_revision,
                reviewer_kind="authorized_human",
                reviewer_subject_id=auth.subject_id,
                approve=approve,
                reason_code=reason_code,
            )
            updated = conn.execute(
                text(
                    "UPDATE refund_request SET review_status=:status,"
                    " review_revision=:revision, reviewer_subject_id=:reviewer,"
                    " review_reason_code=:reason_code, updated_at=UTC_TIMESTAMP(6)"
                    " WHERE tenant_id=:tenant_id AND refund_request_id=:refund_request_id"
                    " AND review_status=:old_status AND review_revision=:old_revision"
                ),
                {
                    "status": changed.status,
                    "revision": changed.revision,
                    "reviewer": changed.reviewer_subject_id,
                    "reason_code": changed.reason_code,
                    "tenant_id": auth.tenant_id,
                    "refund_request_id": refund_request_id,
                    "old_status": review.status,
                    "old_revision": review.revision,
                },
            )
            if updated.rowcount != 1:
                raise RefundSubmissionConflict("退款审核版本冲突")
            if not approve:
                guard = conn.execute(
                    text(
                        "UPDATE refund_order_guard SET active_request_id=NULL,"
                        " reserved_goods_minor=0,reserved_shipping_minor=0,"
                        " revision=revision+1,updated_at=UTC_TIMESTAMP(6)"
                        " WHERE tenant_id=:tenant_id AND order_id=:order_id"
                        " AND active_request_id=:refund_request_id"
                        " AND reserved_goods_minor=:goods_minor"
                        " AND reserved_shipping_minor=:shipping_minor"
                    ),
                    {
                        "tenant_id": auth.tenant_id,
                        "order_id": row.order_id,
                        "refund_request_id": refund_request_id,
                        "goods_minor": int(row.goods_minor),
                        "shipping_minor": int(row.shipping_minor),
                    },
                )
                if guard.rowcount != 1:
                    raise RefundSubmissionConflict("退款审核释放占用失败，已回滚审核结果")
            if not idempotency.complete(idem, "succeeded", refund_request_id):
                raise RefundSubmissionConflict("退款审核幂等结果写入冲突")
            if approve:
                assert self._outbox is not None
                event = EventEnvelope(
                    schema_version="1.0",
                    event_id=uuid.uuid4().hex,
                    event_type="refund.approved",
                    event_version=1,
                    tenant_id=auth.tenant_id,
                    request_id=trace.request_id,
                    correlation_id=trace.correlation_id,
                    trace_id=trace.trace_id,
                    operation_id=uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"{auth.tenant_id}:{auth.subject_id}:refund_review:{idempotency_key}",
                    ).hex,
                    causation_id=None,
                    parent_event_id=None,
                    producer="refund-review-use-case",
                    occurred_at=datetime.now(timezone.utc).isoformat(),
                    idempotency_key=f"refund-approved:{refund_request_id}:{changed.revision}",
                    subject_id=auth.subject_id,
                    retry_count=0,
                    payload=RefundApprovedPayload(
                        refund_request_id=refund_request_id,
                        revision=changed.revision,
                        approved_amount_minor=int(row.amount_minor),
                        currency=row.currency,
                        return_status="unknown",
                        review_subject_id=auth.subject_id,
                        actor_subject_id=row.actor_subject_id,
                        customer_id=row.customer_id,
                    ),
                )
                self._outbox.enqueue(
                    conn,
                    event,
                    aggregate_type="refund_request",
                    aggregate_id=refund_request_id,
                    thread_id=trace.thread_id,
                )
            conn.execute(
                text(
                    "INSERT INTO agent_audit_log (audit_id,tenant_id,request_id,"
                    "correlation_id,trace_id,thread_id,actor_subject_id,customer_id,"
                    "action,resource_refs,parameter_summary,result_summary,status)"
                    " VALUES (:audit_id,:tenant_id,:request_id,:correlation_id,:trace_id,"
                    ":thread_id,:actor_subject_id,:customer_id,'refund.review',"
                    ":resource_refs,:parameter_summary,:result_summary,'success')"
                ),
                {
                    "audit_id": uuid.uuid4().hex,
                    "tenant_id": auth.tenant_id,
                    "request_id": trace.request_id,
                    "correlation_id": trace.correlation_id,
                    "trace_id": trace.trace_id,
                    "thread_id": trace.thread_id,
                    "actor_subject_id": auth.subject_id,
                    "customer_id": row.customer_id,
                    "resource_refs": json.dumps([f"refund_request:{refund_request_id}"]),
                    "parameter_summary": json.dumps(
                        {
                            "decision": "approved" if approve else "rejected",
                            "expected_revision": expected_revision,
                            "reason_code": reason_code,
                        }
                    ),
                    "result_summary": json.dumps(
                        {
                            "review_status": changed.status,
                            "review_revision": changed.revision,
                        }
                    ),
                },
            )
            return changed
