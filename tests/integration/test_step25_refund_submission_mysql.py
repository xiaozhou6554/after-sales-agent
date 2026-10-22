"""本地合成回放：报价、确认、申请/占用/Outbox 在同一真实 MySQL 事务。"""

import json
from dataclasses import replace
from datetime import date, datetime, timezone

import pytest
from bootstrap.settings import load_settings
from sqlalchemy import event, text

from application.common.authorization import AuthorizationDenied
from application.common.context import AuthContext, DataScope, TraceContext
from application.common.events import parse_event
from application.common.ports.idempotency import IdempotencyConflictError
from application.refund.confirmation import RefundConfirmationUseCase
from application.refund.qualification import RefundQualificationUseCase
from application.refund.query import RefundRequestQueryUseCase
from application.refund.submission import RefundSubmissionUseCase
from domain.refund.review import ReviewConflict
from infrastructure.messaging.event_contract import EventCatalog
from infrastructure.messaging.outbox_repository import OutboxRepository
from infrastructure.mock.refund_snapshot_adapter import MockRefundSnapshotAdapter
from infrastructure.persistence.mysql.connection import create_mysql_engine
from infrastructure.persistence.mysql.repositories.refund_quote_repository import (
    MySqlRefundQuoteRepository,
)
from infrastructure.persistence.mysql.repositories.refund_request_read_repository import (
    MySqlRefundRequestReadRepository,
)
from infrastructure.refund.human_review import MySqlMockRefundHumanReview
from infrastructure.refund.review_handler import MockRefundReviewHandler
from infrastructure.refund.submission_adapter import (
    MySqlMockRefundSubmissionAdapter,
    RefundSubmissionConflict,
)
from infrastructure.security.confirmation_store import MySQLConfirmationStore


def test_confirmed_request_and_outbox_commit_once_and_replay() -> None:
    settings = load_settings()
    engine = create_mysql_engine(
        settings.mysql_url, settings.mysql_connect_timeout_s, settings.mysql_read_timeout_s
    )
    connection = engine.connect()
    transaction = connection.begin()
    try:
        now = datetime.now(timezone.utc)
        auth = AuthContext(
            tenant_id="tenant-north",
            subject_id="synthetic-customer-refund-test",
            roles=("customer",),
            scopes=("refund:read", "refund:request"),
            data_scope=DataScope(customer_ids=("SYN-CUSTOMER-N-01-001",)),
            session_id="refund-integration",
            issued_at=now,
            expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        )
        trace = TraceContext(
            "1.0",
            "req-refund-integration",
            "corr-refund-integration",
            "trace-refund-integration",
            "thread-refund-integration",
        )
        snapshots = MockRefundSnapshotAdapter(engine, as_of=date(2026, 9, 3)).bind(connection)
        quotes = MySqlRefundQuoteRepository(engine).bind(connection)
        _, quote = RefundQualificationUseCase(snapshots, quotes).calculate_quote(
            auth, "SYN-ORDER-N-01-001", "changed_mind"
        )
        assert quote is not None and quote.amount_minor == 39402
        catalog = EventCatalog(
            "data/contracts/event-catalog.json", "data/contracts/events.schema.json"
        )
        adapter = MySqlMockRefundSubmissionAdapter(
            engine, snapshots, OutboxRepository(engine, catalog)
        ).bind(connection)
        service = RefundSubmissionUseCase(quotes, adapter)
        confirmations = MySQLConfirmationStore(engine).bind(connection)
        confirmation_flow = RefundConfirmationUseCase(service, confirmations)
        prepared = confirmation_flow.prepare(auth, quote.quote_id)
        assert prepared.record.expires_at <= quote.expires_at
        confirmation_flow.verify(auth, prepared.record.confirmation_id, prepared.mock_challenge)
        _, alternate_quote = RefundQualificationUseCase(snapshots, quotes).calculate_quote(
            auth, quote.order_id, quote.reason_code
        )
        assert alternate_quote is not None and alternate_quote.quote_id != quote.quote_id
        alternate_confirmation = confirmation_flow.prepare(auth, alternate_quote.quote_id)
        confirmation_flow.verify(
            auth,
            alternate_confirmation.record.confirmation_id,
            alternate_confirmation.mock_challenge,
        )
        original_version = connection.execute(
            text(
                "SELECT version FROM mock_oms_order WHERE tenant_id=:tenant_id"
                " AND order_id=:order_id"
            ),
            {"tenant_id": auth.tenant_id, "order_id": quote.order_id},
        ).scalar_one()
        connection.execute(
            text(
                "UPDATE mock_oms_order SET version=version+1 WHERE tenant_id=:tenant_id"
                " AND order_id=:order_id"
            ),
            {"tenant_id": auth.tenant_id, "order_id": quote.order_id},
        )
        with pytest.raises(RefundSubmissionConflict):
            service.submit(
                auth,
                quote.quote_id,
                prepared.record.confirmation_id,
                "idem-refund-integration",
                trace,
            )
        assert (
            confirmations.get(auth.tenant_id, prepared.record.confirmation_id).status == "verified"
        )
        connection.execute(
            text(
                "UPDATE mock_oms_order SET version=:version WHERE tenant_id=:tenant_id"
                " AND order_id=:order_id"
            ),
            {"version": original_version, "tenant_id": auth.tenant_id, "order_id": quote.order_id},
        )
        first = service.submit(
            auth,
            quote.quote_id,
            prepared.record.confirmation_id,
            "idem-refund-integration",
            trace,
        )
        second = service.submit(
            auth,
            quote.quote_id,
            prepared.record.confirmation_id,
            "idem-refund-integration",
            trace,
        )
        assert first.status == "pending_review" and not first.replayed
        assert second.refund_request_id == first.refund_request_id and second.replayed
        rows = connection.execute(
            text(
                "SELECT COUNT(*) FROM refund_request_line WHERE tenant_id=:tenant_id"
                " AND refund_request_id=:request_id"
            ),
            {"tenant_id": auth.tenant_id, "request_id": first.refund_request_id},
        ).scalar_one()
        assert rows == 1
        guard = connection.execute(
            text(
                "SELECT active_request_id,reserved_goods_minor,reserved_shipping_minor,revision"
                " FROM refund_order_guard WHERE tenant_id=:tenant_id AND order_id=:order_id"
            ),
            {"tenant_id": auth.tenant_id, "order_id": quote.order_id},
        ).one()
        assert guard.active_request_id == first.refund_request_id
        assert (guard.reserved_goods_minor, guard.reserved_shipping_minor, guard.revision) == (
            39402,
            0,
            1,
        )
        outbox = connection.execute(
            text(
                "SELECT event_type,status FROM outbox_event WHERE tenant_id=:tenant_id"
                " AND aggregate_type='refund_request' AND aggregate_id=:request_id"
            ),
            {"tenant_id": auth.tenant_id, "request_id": first.refund_request_id},
        ).all()
        assert len(outbox) == 1 and outbox[0].event_type == "refund.confirmed"
        assert outbox[0].status == "pending"
        assert (
            confirmations.get(auth.tenant_id, prepared.record.confirmation_id).status == "consumed"
        )
        with pytest.raises(IdempotencyConflictError):
            service.submit(
                auth,
                alternate_quote.quote_id,
                alternate_confirmation.record.confirmation_id,
                "idem-refund-integration",
                trace,
            )
        assert (
            confirmations.get(auth.tenant_id, alternate_confirmation.record.confirmation_id).status
            == "verified"
        )
        another = confirmation_flow.prepare(auth, quote.quote_id)
        confirmation_flow.verify(auth, another.record.confirmation_id, another.mock_challenge)
        with pytest.raises(RefundSubmissionConflict):
            service.submit(
                auth,
                quote.quote_id,
                another.record.confirmation_id,
                "idem-refund-different-key",
                trace,
            )
        assert (
            confirmations.get(auth.tenant_id, another.record.confirmation_id).status == "verified"
        )
        pending_key = connection.execute(
            text(
                "SELECT COUNT(*) FROM operation_idempotency WHERE tenant_id=:tenant_id"
                " AND idempotency_key='idem-refund-different-key'"
            ),
            {"tenant_id": auth.tenant_id},
        ).scalar_one()
        assert pending_key == 0
        guard_after = connection.execute(
            text(
                "SELECT active_request_id,revision FROM refund_order_guard"
                " WHERE tenant_id=:tenant_id AND order_id=:order_id"
            ),
            {"tenant_id": auth.tenant_id, "order_id": quote.order_id},
        ).one()
        assert guard_after.active_request_id == first.refund_request_id
        assert guard_after.revision == 1
        event_payload = connection.execute(
            text(
                "SELECT payload FROM outbox_event WHERE tenant_id=:tenant_id"
                " AND aggregate_type='refund_request' AND aggregate_id=:request_id"
            ),
            {"tenant_id": auth.tenant_id, "request_id": first.refund_request_id},
        ).scalar_one()
        review_event = parse_event(json.loads(event_payload))
        handler = MockRefundReviewHandler(snapshots)
        assert handler(connection, review_event) == first.refund_request_id
        assert handler(connection, review_event) == first.refund_request_id
        state = connection.execute(
            text(
                "SELECT review_status,review_revision FROM refund_request"
                " WHERE tenant_id=:tenant_id AND refund_request_id=:request_id"
            ),
            {"tenant_id": auth.tenant_id, "request_id": first.refund_request_id},
        ).one()
        assert state.review_status == "reviewing" and state.review_revision == 1
        query = RefundRequestQueryUseCase(MySqlRefundRequestReadRepository(engine).bind(connection))
        result = query.by_id(auth, first.refund_request_id)
        assert result.review_status == "reviewing" and result.revision == 2
        assert result.return_status == "unknown" and result.funds_status == "unknown"
        assert (
            query.by_original_key(auth, "idem-refund-integration").refund_request_id
            == first.refund_request_id
        )
        with pytest.raises(AuthorizationDenied):
            query.by_id(
                replace(auth, data_scope=DataScope(customer_ids=("OTHER",))),
                first.refund_request_id,
            )
        with pytest.raises(AuthorizationDenied):
            query.by_original_key(
                replace(auth, data_scope=DataScope(customer_ids=("OTHER",))),
                "idem-refund-integration",
            )
        reviewer = replace(
            auth,
            subject_id="mock-human-reviewer",
            roles=("support",),
            scopes=("refund:review",),
        )
        human = MySqlMockRefundHumanReview(engine, snapshots).bind(connection)
        with pytest.raises(RefundSubmissionConflict):
            human.decide(
                reviewer,
                first.refund_request_id,
                expected_revision=1,
                approve=True,
                reason_code="MANUAL_REVIEW_PASSED",
                idempotency_key="idem-refund-no-outbox",
                trace=trace,
            )

        def fail_audit(_conn, _cursor, statement, _parameters, _context, _executemany):
            if "INSERT INTO agent_audit_log" in statement:
                raise RuntimeError("synthetic audit outage")

        event.listen(engine, "before_cursor_execute", fail_audit)
        try:
            with pytest.raises(RuntimeError, match="synthetic audit outage"):
                human.decide(
                    reviewer,
                    first.refund_request_id,
                    expected_revision=1,
                    approve=False,
                    reason_code="AUDIT_FAIL_TEST",
                    idempotency_key="idem-refund-audit-fail",
                    trace=trace,
                )
        finally:
            event.remove(engine, "before_cursor_execute", fail_audit)
        unchanged = connection.execute(
            text(
                "SELECT review_status,review_revision FROM refund_request"
                " WHERE tenant_id=:tenant_id AND refund_request_id=:request_id"
            ),
            {"tenant_id": auth.tenant_id, "request_id": first.refund_request_id},
        ).one()
        assert (unchanged.review_status, unchanged.review_revision) == ("reviewing", 1)
        assert (
            connection.execute(
                text(
                    "SELECT COUNT(*) FROM operation_idempotency"
                    " WHERE tenant_id=:tenant_id AND idempotency_key=:key"
                ),
                {"tenant_id": auth.tenant_id, "key": "idem-refund-audit-fail"},
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                text(
                    "SELECT active_request_id FROM refund_order_guard"
                    " WHERE tenant_id=:tenant_id AND order_id=:order_id"
                ),
                {"tenant_id": auth.tenant_id, "order_id": quote.order_id},
            ).scalar_one()
            == first.refund_request_id
        )
        with pytest.raises(AuthorizationDenied):
            human.decide(
                replace(auth, scopes=("refund:review",)),
                first.refund_request_id,
                expected_revision=1,
                approve=False,
                reason_code="FORGED_REVIEW",
                idempotency_key="idem-refund-forged-review",
                trace=trace,
            )
        rejected = human.decide(
            reviewer,
            first.refund_request_id,
            expected_revision=1,
            approve=False,
            reason_code="INSPECTION_REQUIRED_UNMET",
            idempotency_key="idem-refund-human-review",
            trace=trace,
        )
        assert rejected.status == "rejected" and rejected.revision == 2
        replayed_review = human.decide(
            reviewer,
            first.refund_request_id,
            expected_revision=1,
            approve=False,
            reason_code="INSPECTION_REQUIRED_UNMET",
            idempotency_key="idem-refund-human-review",
            trace=trace,
        )
        assert replayed_review == rejected
        audits = connection.execute(
            text(
                "SELECT COUNT(*) FROM agent_audit_log WHERE tenant_id=:tenant_id"
                " AND action='refund.review' AND trace_id=:trace_id"
            ),
            {"tenant_id": auth.tenant_id, "trace_id": trace.trace_id},
        ).scalar_one()
        assert audits == 1
        released = connection.execute(
            text(
                "SELECT active_request_id,reserved_goods_minor,reserved_shipping_minor,revision"
                " FROM refund_order_guard WHERE tenant_id=:tenant_id AND order_id=:order_id"
            ),
            {"tenant_id": auth.tenant_id, "order_id": quote.order_id},
        ).one()
        assert released.active_request_id is None
        assert (
            released.reserved_goods_minor,
            released.reserved_shipping_minor,
            released.revision,
        ) == (0, 0, 2)
        with pytest.raises(ReviewConflict):
            human.decide(
                reviewer,
                first.refund_request_id,
                expected_revision=2,
                approve=False,
                reason_code="REPLAY",
                idempotency_key="idem-refund-human-review-new",
                trace=trace,
            )
        _, next_quote = RefundQualificationUseCase(snapshots, quotes).calculate_quote(
            auth, quote.order_id, quote.reason_code
        )
        assert next_quote is not None and next_quote.quote_id != quote.quote_id
        next_confirmation = confirmation_flow.prepare(auth, next_quote.quote_id)
        confirmation_flow.verify(
            auth, next_confirmation.record.confirmation_id, next_confirmation.mock_challenge
        )

        class CommitThenTimeout:
            def commit(self, *args):
                adapter.commit(*args)
                raise TimeoutError("synthetic response lost after local commit")

        with pytest.raises(TimeoutError, match="synthetic response lost"):
            RefundSubmissionUseCase(quotes, CommitThenTimeout()).submit(
                auth,
                next_quote.quote_id,
                next_confirmation.record.confirmation_id,
                "idem-refund-after-rejection",
                trace,
            )
        recovered = query.by_original_key(auth, "idem-refund-after-rejection")
        next_request = service.submit(
            auth,
            next_quote.quote_id,
            next_confirmation.record.confirmation_id,
            "idem-refund-after-rejection",
            trace,
        )
        assert next_request.replayed
        assert next_request.refund_request_id == recovered.refund_request_id
        next_payload = connection.execute(
            text(
                "SELECT payload FROM outbox_event WHERE tenant_id=:tenant_id"
                " AND aggregate_type='refund_request' AND aggregate_id=:request_id"
                " AND event_type='refund.confirmed'"
            ),
            {"tenant_id": auth.tenant_id, "request_id": next_request.refund_request_id},
        ).scalar_one()
        handler(connection, parse_event(json.loads(next_payload)))
        approving = MySqlMockRefundHumanReview(
            engine, snapshots, OutboxRepository(engine, catalog)
        ).bind(connection)
        approved = approving.decide(
            reviewer,
            next_request.refund_request_id,
            expected_revision=1,
            approve=True,
            reason_code="MANUAL_REVIEW_PASSED",
            idempotency_key="idem-refund-human-approved",
            trace=trace,
        )
        assert approved.status == "approved" and approved.revision == 2
        assert (
            approving.decide(
                reviewer,
                next_request.refund_request_id,
                expected_revision=1,
                approve=True,
                reason_code="MANUAL_REVIEW_PASSED",
                idempotency_key="idem-refund-human-approved",
                trace=trace,
            )
            == approved
        )
        approved_payloads = (
            connection.execute(
                text(
                    "SELECT payload FROM outbox_event WHERE tenant_id=:tenant_id"
                    " AND aggregate_type='refund_request' AND aggregate_id=:request_id"
                    " AND event_type='refund.approved'"
                ),
                {"tenant_id": auth.tenant_id, "request_id": next_request.refund_request_id},
            )
            .scalars()
            .all()
        )
        assert len(approved_payloads) == 1
        approved_event = parse_event(json.loads(approved_payloads[0]))
        assert approved_event.payload.approved_amount_minor == 39402
        assert approved_event.payload.review_subject_id == reviewer.subject_id
        assert (
            connection.execute(
                text(
                    "SELECT COUNT(*) FROM mock_pay_refund_execution"
                    " WHERE tenant_id=:tenant_id AND order_id=:order_id"
                ),
                {"tenant_id": auth.tenant_id, "order_id": quote.order_id},
            ).scalar_one()
            == 0
        )
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()
