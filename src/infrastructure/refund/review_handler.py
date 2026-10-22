"""refund.confirmed 的受信任审核处理器；由 InboxConsumer 同事务调用。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Connection

from application.common.events import (
    EventEnvelope,
    RefundApprovedPayload,
    RefundConfirmedPayload,
)
from domain.refund.review import RefundReview, decide_review, start_review
from infrastructure.messaging.inbox_repository import InboxConflictError
from infrastructure.messaging.outbox_repository import OutboxRepository
from infrastructure.mock.refund_snapshot_adapter import MockRefundSnapshotAdapter


class MockRefundReviewHandler:
    def __init__(
        self, snapshots: MockRefundSnapshotAdapter, outbox: OutboxRepository | None = None
    ) -> None:
        self._snapshots = snapshots
        self._outbox = outbox

    def __call__(self, connection: Connection, event: EventEnvelope) -> str:
        if event.event_type != "refund.confirmed" or not isinstance(
            event.payload, RefundConfirmedPayload
        ):
            raise InboxConflictError("退款审核收到错误事件类型")
        payload = event.payload
        row = (
            connection.execute(
                text(
                    "SELECT refund_request_id,order_id,customer_id,actor_subject_id,"
                    "review_status,review_revision,eligibility,amount_minor,goods_minor,"
                    "shipping_minor,currency,order_version"
                    " FROM refund_request WHERE tenant_id=:tenant_id"
                    " AND refund_request_id=:refund_request_id FOR UPDATE"
                ),
                {"tenant_id": event.tenant_id, "refund_request_id": payload.refund_request_id},
            )
            .mappings()
            .first()
        )
        if row is None or (
            row["order_id"] != payload.order_id
            or row["customer_id"] != payload.customer_id
            or row["actor_subject_id"] != payload.actor_subject_id
            or event.subject_id != payload.actor_subject_id
            or int(row["amount_minor"]) != payload.amount_minor
            or row["currency"] != payload.currency
            or payload.revision != 0
            or payload.requires_manual_review
            != ("true" if row["eligibility"] == "manual_review" else "false")
        ):
            raise InboxConflictError("退款审核事件与申请身份、金额或初始版本不一致")
        if row["review_status"] in ("approved", "rejected"):
            return payload.refund_request_id
        snapshot = self._snapshots.bind(connection).load(event.tenant_id, payload.order_id)
        if snapshot is None or snapshot.customer_id != row["customer_id"]:
            raise InboxConflictError("退款审核来源快照不可用")
        review = RefundReview(
            payload.refund_request_id,
            row["review_status"],
            int(row["review_revision"]),
            row["eligibility"],
            snapshot.facts.fulfillment_status,
        )
        if review.status == "pending_review":
            changed = start_review(review, expected_revision=review.revision)
            self._save(connection, event.tenant_id, review, changed)
            review = changed
        # 只有明确符合资格且未锁履约、来源一致的 Mock 待发货申请可自动通过。
        if (
            review.eligibility == "eligible"
            and snapshot.facts.order_version == row["order_version"]
            and snapshot.facts.active_request_id == payload.refund_request_id
            and snapshot.facts.paid_goods_minor == int(row["goods_minor"])
            and snapshot.facts.paid_shipping_minor == int(row["shipping_minor"])
            and snapshot.facts.refunded_goods_minor == 0
            and snapshot.facts.refunded_shipping_minor == 0
            and snapshot.facts.reserved_goods_minor == int(row["goods_minor"])
            and snapshot.facts.reserved_shipping_minor == int(row["shipping_minor"])
            and snapshot.facts.fulfillment_status == "pending_shipment"
            and not snapshot.facts.fulfillment_locked
            and not snapshot.facts.status_conflict
            and snapshot.facts.payment_status == "succeeded"
            and self._outbox is not None
        ):
            changed = decide_review(
                review,
                expected_revision=review.revision,
                reviewer_kind="mock_consumer",
                reviewer_subject_id="mock-refund-review",
                approve=True,
                reason_code="MOCK_PRE_SHIPMENT_CLEAR",
            )
            self._save(connection, event.tenant_id, review, changed)
            assert self._outbox is not None
            approved_event = EventEnvelope(
                schema_version="1.0",
                event_id=uuid.uuid4().hex,
                event_type="refund.approved",
                event_version=1,
                tenant_id=event.tenant_id,
                request_id=event.request_id,
                correlation_id=event.correlation_id,
                trace_id=event.trace_id,
                operation_id=event.operation_id,
                causation_id=event.event_id,
                parent_event_id=event.event_id,
                producer="refund-review-use-case",
                occurred_at=datetime.now(timezone.utc).isoformat(),
                idempotency_key=(f"refund-approved:{payload.refund_request_id}:{changed.revision}"),
                subject_id="mock-refund-review",
                retry_count=0,
                payload=RefundApprovedPayload(
                    refund_request_id=payload.refund_request_id,
                    revision=changed.revision,
                    approved_amount_minor=int(row["amount_minor"]),
                    currency=row["currency"],
                    return_status="unknown",
                    review_subject_id="mock-refund-review",
                    actor_subject_id=row["actor_subject_id"],
                    customer_id=row["customer_id"],
                ),
            )
            self._outbox.enqueue(
                connection,
                approved_event,
                aggregate_type="refund_request",
                aggregate_id=payload.refund_request_id,
            )
        return payload.refund_request_id

    @staticmethod
    def _save(
        connection: Connection, tenant_id: str, previous: RefundReview, changed: RefundReview
    ) -> None:
        updated = connection.execute(
            text(
                "UPDATE refund_request SET review_status=:status,"
                " review_revision=:revision, reviewer_subject_id=:reviewer_subject_id,"
                " review_reason_code=:reason_code, updated_at=UTC_TIMESTAMP(6)"
                " WHERE tenant_id=:tenant_id AND refund_request_id=:refund_request_id"
                " AND review_status=:old_status AND review_revision=:old_revision"
            ),
            {
                "status": changed.status,
                "revision": changed.revision,
                "reviewer_subject_id": changed.reviewer_subject_id,
                "reason_code": changed.reason_code,
                "tenant_id": tenant_id,
                "refund_request_id": changed.refund_request_id,
                "old_status": previous.status,
                "old_revision": previous.revision,
            },
        )
        if updated.rowcount != 1:
            raise InboxConflictError("退款审核并发版本冲突")
