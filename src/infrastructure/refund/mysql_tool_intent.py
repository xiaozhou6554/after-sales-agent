"""Session-bound resolver for verified local refund confirmations."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine

from application.common.context import AuthContext
from application.common.ports.confirmation import ConfirmationConflictError
from application.refund.qualification import RefundQuote
from application.refund.submission import refund_confirmation_binding
from application.refund.tool_intent import RefundToolIntent
from infrastructure.security.confirmation_store import MySQLConfirmationStore


class MySqlRefundToolIntent:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def register(self, auth: AuthContext, quote: RefundQuote, confirmation_id: str) -> None:
        if (
            not auth.session_id
            or len(auth.session_id) > 128
            or quote.tenant_id != auth.tenant_id
            or quote.subject_id != auth.subject_id
        ):
            raise ConfirmationConflictError("refund tool intent identity mismatch")
        record = MySQLConfirmationStore(self._engine).get(auth.tenant_id, confirmation_id)
        if (
            record is None
            or record.status != "prepared"
            or record.binding != refund_confirmation_binding(auth, quote)
        ):
            raise ConfirmationConflictError("refund confirmation is not bound to quote")
        with self._engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO refund_tool_intent"
                    " (tenant_id,confirmation_id,actor_subject_id,session_id,quote_id,"
                    "order_id,reason_code,created_at) VALUES"
                    " (:tenant,:confirmation,:actor,:session,:quote,:order,:reason,:now)"
                ),
                {
                    "tenant": auth.tenant_id,
                    "confirmation": confirmation_id,
                    "actor": auth.subject_id,
                    "session": auth.session_id,
                    "quote": quote.quote_id,
                    "order": quote.order_id,
                    "reason": quote.reason_code,
                    "now": datetime.now(timezone.utc).replace(tzinfo=None),
                },
            )

    def resolve(self, auth: AuthContext, order_id: str, reason_code: str) -> RefundToolIntent:
        if (
            not auth.session_id
            or len(auth.session_id) > 128
            or not order_id
            or not reason_code
            or "customer" not in auth.roles
            or len(auth.data_scope.customer_ids) != 1
        ):
            raise ConfirmationConflictError("refund tool intent unavailable")
        with self._engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT i.quote_id,i.confirmation_id,c.status"
                    " FROM refund_tool_intent i"
                    " JOIN operation_confirmation c ON c.tenant_id=i.tenant_id"
                    " AND c.confirmation_id=i.confirmation_id"
                    " JOIN refund_quote q ON q.tenant_id=i.tenant_id"
                    " AND q.quote_id=i.quote_id"
                    " WHERE i.tenant_id=:tenant AND i.actor_subject_id=:actor"
                    " AND i.session_id=:session AND i.order_id=:order"
                    " AND i.reason_code=:reason AND c.actor_subject_id=:actor"
                    " AND c.confirmer_subject_id=:actor AND c.customer_id=:customer"
                    " AND c.operation_type='refund_request'"
                    " AND c.target_id=:order AND q.customer_id=:customer"
                    " AND q.actor_subject_id=:actor AND q.order_id=:order"
                    " AND q.reason_code=:reason AND c.payload_digest IS NOT NULL"
                    " AND c.status IN ('verified','consumed')"
                    " AND c.expires_at>:now AND q.expires_at>:now"
                    " ORDER BY (c.status='verified') DESC, c.created_at DESC LIMIT 3"
                ),
                {
                    "tenant": auth.tenant_id,
                    "actor": auth.subject_id,
                    "session": auth.session_id,
                    "order": order_id,
                    "reason": reason_code,
                    "customer": auth.data_scope.customer_ids[0],
                    "now": datetime.now(timezone.utc).replace(tzinfo=None),
                },
            ).all()
        verified = [row for row in rows if row.status == "verified"]
        candidates = verified if verified else rows
        if len(candidates) != 1:
            raise ConfirmationConflictError("refund tool intent missing or ambiguous")
        row = candidates[0]
        return RefundToolIntent(
            quote_id=row.quote_id,
            confirmation_id=row.confirmation_id,
            idempotency_key=uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{auth.tenant_id}:{auth.subject_id}:refund_request:{row.confirmation_id}",
            ).hex,
        )
