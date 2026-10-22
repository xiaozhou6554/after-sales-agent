"""本地 Mock 退款申请事务：重核事实、确认消费、占用和 Outbox 原子提交。"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from application.common.authorization import require_customer_access, verify_tool_context
from application.common.context import AuthContext, OperationContext, TraceContext
from application.common.events import EventEnvelope, RefundConfirmedPayload
from application.refund.qualification import RefundLineAllocation, RefundQuote
from application.refund.submission import RefundRequestResult, refund_confirmation_binding
from domain.refund import assess_refund
from infrastructure.messaging.idempotency_store import MySQLIdempotencyStore
from infrastructure.messaging.outbox_repository import OutboxRepository
from infrastructure.mock.refund_snapshot_adapter import MockRefundSnapshotAdapter
from infrastructure.persistence.mysql.repositories.refund_quote_repository import (
    MySqlRefundQuoteRepository,
)
from infrastructure.security.confirmation_store import MySQLConfirmationStore


class RefundSubmissionConflict(ValueError):
    pass


def _utc_naive(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None)


class MySqlMockRefundSubmissionAdapter:
    """仅用于合成 Mock-v2；企业接入需替换权威快照和跨系统余额守卫。"""

    def __init__(
        self,
        engine,
        snapshots: MockRefundSnapshotAdapter,
        outbox: OutboxRepository,
        *,
        clock=lambda: datetime.now(timezone.utc),
        connection: Connection | None = None,
    ) -> None:
        self._engine = engine
        self._snapshots = snapshots
        self._outbox = outbox
        self._clock = clock
        self._connection = connection

    def bind(self, connection: Connection) -> "MySqlMockRefundSubmissionAdapter":
        return MySqlMockRefundSubmissionAdapter(
            self._engine,
            self._snapshots,
            self._outbox,
            clock=self._clock,
            connection=connection,
        )

    @contextmanager
    def _transaction(self):
        if self._connection is not None:
            with self._connection.begin_nested():
                yield self._connection
        else:
            with self._engine.begin() as connection:
                yield connection

    def commit(
        self,
        auth: AuthContext,
        quote: RefundQuote,
        binding: OperationContext,
        confirmation_id: str,
        idempotency_key: str,
        trace: TraceContext,
    ) -> RefundRequestResult:
        if (
            not quote.lines
            or not trace.request_id
            or not trace.correlation_id
            or not trace.trace_id
        ):
            raise RefundSubmissionConflict("退款申请缺少逐行报价或可信链路标识")
        verify_tool_context(auth, ("refund:request",))
        require_customer_access(auth, quote.beneficiary_customer_id, tenant_id=quote.tenant_id)
        if (
            quote.tenant_id != auth.tenant_id
            or quote.subject_id != auth.subject_id
            or "customer" not in auth.roles
            or auth.data_scope.customer_ids != (quote.beneficiary_customer_id,)
            or binding != refund_confirmation_binding(auth, quote)
        ):
            raise RefundSubmissionConflict("退款确认绑定身份或内容不一致")
        now = self._clock()
        operation_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{auth.tenant_id}:{auth.subject_id}:refund_request:{idempotency_key}",
        ).hex
        try:
            with self._transaction() as conn:
                order = conn.execute(
                    text(
                        "SELECT version FROM mock_oms_order WHERE tenant_id=:tenant_id"
                        " AND order_id=:order_id"
                    ),
                    {"tenant_id": auth.tenant_id, "order_id": quote.order_id},
                ).first()
                if order is None:
                    raise RefundSubmissionConflict("订单不可用或不在授权范围内")
                conn.execute(
                    text(
                        "INSERT IGNORE INTO refund_order_guard"
                        " (tenant_id,order_id,active_request_id,reserved_goods_minor,"
                        "reserved_shipping_minor,currency,revision,created_at,updated_at)"
                        " VALUES (:tenant_id,:order_id,NULL,0,0,:currency,0,:now,:now)"
                    ),
                    {
                        "tenant_id": auth.tenant_id,
                        "order_id": quote.order_id,
                        "currency": quote.currency,
                        "now": _utc_naive(now),
                    },
                )
                guard = conn.execute(
                    text(
                        "SELECT active_request_id,reserved_goods_minor,"
                        "reserved_shipping_minor,currency,revision FROM refund_order_guard"
                        " WHERE tenant_id=:tenant_id AND order_id=:order_id FOR UPDATE"
                    ),
                    {"tenant_id": auth.tenant_id, "order_id": quote.order_id},
                ).first()
                conn.execute(
                    text(
                        "SELECT payment_id FROM mock_pay_payment WHERE tenant_id=:tenant_id"
                        " AND order_id=:order_id FOR UPDATE"
                    ),
                    {"tenant_id": auth.tenant_id, "order_id": quote.order_id},
                ).all()
                idempotency = MySQLIdempotencyStore(self._engine).bind(conn)
                record = idempotency.reserve(
                    auth.tenant_id,
                    auth.subject_id,
                    "refund_request",
                    idempotency_key,
                    binding.payload_digest,
                )
                if record.status == "succeeded" and record.result_ref:
                    return self._existing(conn, auth, record.result_ref, replayed=True)
                if record.status != "pending":
                    raise RefundSubmissionConflict("原退款操作结果待核实，请按原标识查询")
                if (
                    guard is None
                    or guard.active_request_id is not None
                    or int(guard.reserved_goods_minor) != 0
                    or int(guard.reserved_shipping_minor) != 0
                    or guard.currency != quote.currency
                ):
                    raise RefundSubmissionConflict("订单已有活动退款申请或占用状态异常")

                stored = (
                    MySqlRefundQuoteRepository(self._engine)
                    .bind(conn)
                    .load(auth.tenant_id, quote.quote_id)
                )
                if stored != quote or quote.expires_at <= now:
                    raise RefundSubmissionConflict("退款报价已变化或过期，请重新报价确认")
                snapshot = self._snapshots.bind(conn).load(auth.tenant_id, quote.order_id)
                if snapshot is None or snapshot.customer_id != quote.beneficiary_customer_id:
                    raise RefundSubmissionConflict("订单归属或可信快照已变化")
                from application.refund.qualification import REASON_CATEGORIES

                facts = replace(
                    snapshot.facts,
                    reason_code=quote.reason_code,
                    reason_category=REASON_CATEGORIES.get(quote.reason_code),
                )
                assessment = assess_refund(facts)
                expected_lines = tuple(
                    RefundLineAllocation(
                        line.line_id,
                        line.quantity,
                        line.goods_minor,
                        line.shipping_minor if assessment.shipping_minor else 0,
                    )
                    for line in snapshot.lines
                )
                by_line_id = {line.line_id: line for line in expected_lines}
                selected_lines_valid = (
                    len({line.line_id for line in quote.lines}) == len(quote.lines)
                    and all(by_line_id.get(line.line_id) == line for line in quote.lines)
                    and sum(line.goods_minor for line in quote.lines) == quote.goods_minor
                    and sum(line.shipping_minor for line in quote.lines) == quote.shipping_minor
                    and quote.amount_minor == quote.goods_minor + quote.shipping_minor
                    and quote.amount_minor > 0
                )
                if (
                    not assessment.can_submit
                    or assessment.eligibility != quote.eligibility
                    or not selected_lines_valid
                    or assessment.amount_minor is None
                    or quote.amount_minor > assessment.amount_minor
                    or assessment.currency != quote.currency
                    or assessment.policy_id != quote.policy_id
                    or assessment.policy_version != quote.policy_version
                    or assessment.order_version != quote.order_version
                    or assessment.payment_version != quote.payment_version
                    or str(order.version) != quote.order_version
                ):
                    raise RefundSubmissionConflict("退款金额、组成或来源版本已变化，请重新报价")

                confirmation = MySQLConfirmationStore(self._engine).bind(conn)
                confirmation.consume(confirmation_id, binding, operation_id, now)
                refund_id = f"refund-{uuid.uuid4().hex}"
                values = {
                    "tenant_id": auth.tenant_id,
                    "refund_request_id": refund_id,
                    "quote_id": quote.quote_id,
                    "order_id": quote.order_id,
                    "actor_subject_id": auth.subject_id,
                    "customer_id": quote.beneficiary_customer_id,
                    "confirmation_id": confirmation_id,
                    "idempotency_key": idempotency_key,
                    "reason_code": quote.reason_code,
                    "eligibility": quote.eligibility,
                    "amount_minor": quote.amount_minor,
                    "goods_minor": quote.goods_minor,
                    "shipping_minor": quote.shipping_minor,
                    "currency": quote.currency,
                    "policy_id": quote.policy_id,
                    "policy_version": quote.policy_version,
                    "order_version": quote.order_version,
                    "payment_version": quote.payment_version,
                    "now": _utc_naive(now),
                }
                conn.execute(
                    text(
                        "INSERT INTO refund_request (tenant_id,refund_request_id,quote_id,"
                        "order_id,actor_subject_id,customer_id,confirmation_id,idempotency_key,"
                        "reason_code,eligibility,review_status,amount_minor,goods_minor,"
                        "shipping_minor,currency,policy_id,policy_version,order_version,"
                        "payment_version,created_at,updated_at) VALUES (:tenant_id,"
                        ":refund_request_id,:quote_id,:order_id,:actor_subject_id,:customer_id,"
                        ":confirmation_id,:idempotency_key,:reason_code,:eligibility,"
                        "'pending_review',:amount_minor,:goods_minor,:shipping_minor,:currency,"
                        ":policy_id,:policy_version,:order_version,:payment_version,:now,:now)"
                    ),
                    values,
                )
                for line in quote.lines:
                    conn.execute(
                        text(
                            "INSERT INTO refund_request_line (tenant_id,refund_request_id,"
                            "line_id,quantity,goods_minor,shipping_minor) VALUES"
                            " (:tenant_id,:refund_request_id,:line_id,:quantity,"
                            ":goods_minor,:shipping_minor)"
                        ),
                        {
                            "tenant_id": auth.tenant_id,
                            "refund_request_id": refund_id,
                            **line.__dict__,
                        },
                    )
                reserved = conn.execute(
                    text(
                        "UPDATE refund_order_guard SET active_request_id=:refund_request_id,"
                        " reserved_goods_minor=:goods_minor,"
                        " reserved_shipping_minor=:shipping_minor, revision=revision+1,"
                        " updated_at=:now WHERE tenant_id=:tenant_id AND order_id=:order_id"
                        " AND revision=:expected_revision AND active_request_id IS NULL"
                        " AND reserved_goods_minor=0 AND reserved_shipping_minor=0"
                    ),
                    {
                        "tenant_id": auth.tenant_id,
                        "order_id": quote.order_id,
                        "refund_request_id": refund_id,
                        "goods_minor": quote.goods_minor,
                        "shipping_minor": quote.shipping_minor,
                        "expected_revision": int(guard.revision),
                        "now": _utc_naive(now),
                    },
                )
                if reserved.rowcount != 1:
                    raise RefundSubmissionConflict("订单退款占用并发冲突")
                event = EventEnvelope(
                    schema_version="1.0",
                    event_id=uuid.uuid4().hex,
                    event_type="refund.confirmed",
                    event_version=1,
                    tenant_id=auth.tenant_id,
                    request_id=trace.request_id,
                    correlation_id=trace.correlation_id,
                    trace_id=trace.trace_id,
                    operation_id=operation_id,
                    causation_id=None,
                    parent_event_id=None,
                    producer="after-sales-service",
                    occurred_at=now.isoformat(),
                    idempotency_key=f"refund-confirmed:{refund_id}",
                    subject_id=auth.subject_id,
                    retry_count=0,
                    payload=RefundConfirmedPayload(
                        refund_request_id=refund_id,
                        order_id=quote.order_id,
                        amount_minor=quote.amount_minor,
                        currency=quote.currency,
                        revision=0,
                        requires_manual_review=(
                            "true" if quote.eligibility == "manual_review" else "false"
                        ),
                        actor_subject_id=auth.subject_id,
                        customer_id=quote.beneficiary_customer_id,
                    ),
                )
                self._outbox.enqueue(
                    conn,
                    event,
                    aggregate_type="refund_request",
                    aggregate_id=refund_id,
                    thread_id=trace.thread_id,
                )
                if not idempotency.complete(record, "succeeded", refund_id):
                    raise RefundSubmissionConflict("退款幂等结果写入冲突")
                return RefundRequestResult(
                    refund_id, "pending_review", quote.amount_minor, quote.currency
                )
        except IntegrityError as exc:
            raise RefundSubmissionConflict("退款申请冲突或已有活动申请，请查询原结果") from exc

    @staticmethod
    def _existing(conn, auth: AuthContext, refund_id: str, *, replayed: bool):
        row = conn.execute(
            text(
                "SELECT review_status,amount_minor,currency FROM refund_request"
                " WHERE tenant_id=:tenant_id AND refund_request_id=:refund_id"
                " AND actor_subject_id=:actor_subject_id"
            ),
            {
                "tenant_id": auth.tenant_id,
                "refund_id": refund_id,
                "actor_subject_id": auth.subject_id,
            },
        ).first()
        if row is None:
            raise RefundSubmissionConflict("原退款申请状态待核实")
        return RefundRequestResult(
            refund_id, row.review_status, int(row.amount_minor), row.currency, replayed
        )
