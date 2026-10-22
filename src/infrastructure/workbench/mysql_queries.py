"""Tenant and customer-scoped employee projections."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine

from application.common.context import AuthContext, OperationContext
from application.common.operation_confirmation import content_digest
from application.workbench.queries import WorkbenchPage
from infrastructure.persistence.mysql.repositories.ticket_repository import MySQLTicketRepository
from infrastructure.security.confirmation_store import MySQLConfirmationStore


class MySQLWorkbenchQueries:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @staticmethod
    def _ids_clause(customer_ids: tuple[str, ...], prefix: str = "customer") -> tuple[str, dict]:
        if not customer_ids:
            return "1=0", {}
        params = {f"{prefix}{index}": value for index, value in enumerate(customer_ids)}
        return f"customer_id IN ({','.join(':' + key for key in params)})", params

    @staticmethod
    def _page(rows: list[dict], offset: int, limit: int) -> WorkbenchPage:
        return WorkbenchPage(rows, offset + limit if len(rows) == limit else None)

    def customers(
        self, tenant_id: str, customer_ids: tuple[str, ...], query: str, offset: int, limit: int
    ) -> WorkbenchPage:
        if not customer_ids:
            return WorkbenchPage([], None)
        needle = query.strip().upper()
        rows = [
            {
                "customer_id": value,
                "display_name_masked": f"客户 {value[:3]}***" if len(value) > 3 else "客户 **",
            }
            for value in customer_ids
            if not needle or needle in value.upper()
        ]
        return self._page(rows[offset : offset + limit], offset, limit)

    def ticket_detail(self, tenant_id: str, ticket_id: str) -> dict | None:
        record = MySQLTicketRepository(self._engine).get(
            tenant_id, ticket_id, include_internal=True
        )
        if record is None:
            return None
        ticket = record.ticket
        return {
            "ticket_id": ticket.ticket_id,
            "customer_id": ticket.customer_id,
            "title": ticket.title,
            "description_masked": ticket.description_masked,
            "status": ticket.status.value,
            "priority": ticket.priority.value,
            "revision": ticket.revision,
            "assigned_to": ticket.assigned_to,
            "messages": [
                {
                    "message_id": item.message_id,
                    "author_subject_id": item.author_subject_id,
                    "content_masked": item.content_masked,
                    "visibility": item.visibility.value,
                }
                for item in ticket.messages
            ],
        }

    def tickets(
        self, tenant_id: str, customer_ids: tuple[str, ...], filters: dict, offset: int, limit: int
    ) -> WorkbenchPage:
        clause, params = self._ids_clause(customer_ids)
        predicates = ["tenant_id=:tenant", clause]
        params.update({"tenant": tenant_id, "offset": offset, "limit": limit})
        for field in ("customer_id", "status", "priority", "assigned_to"):
            value = filters.get(field)
            if value is not None:
                predicates.append(f"{field}=:{field}")
                params[field] = value
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        "SELECT ticket_id,customer_id,status,priority,revision,assigned_to,"
                        "updated_at "
                        "FROM ticket WHERE "
                        + " AND ".join(predicates)
                        + " ORDER BY updated_at DESC,ticket_id LIMIT :limit OFFSET :offset"
                    ),
                    params,
                )
                .mappings()
                .all()
            )
        items = [{**dict(row), "updated_at": row["updated_at"].isoformat()} for row in rows]
        return self._page(items, offset, limit)

    def handoffs(
        self,
        tenant_id: str,
        customer_ids: tuple[str, ...],
        status: str | None,
        offset: int,
        limit: int,
    ) -> WorkbenchPage:
        clause, params = self._ids_clause(customer_ids)
        predicates = ["tenant_id=:tenant", clause]
        params.update({"tenant": tenant_id, "offset": offset, "limit": limit})
        if status is not None:
            predicates.append("status=:status")
            params["status"] = status
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        "SELECT handoff_id,status,revision,customer_id,summary_masked,"
                        "ticket_id,assigned_to "
                        "FROM human_handoff WHERE "
                        + " AND ".join(predicates)
                        + " ORDER BY created_at DESC,handoff_id LIMIT :limit OFFSET :offset"
                    ),
                    params,
                )
                .mappings()
                .all()
            )
        items = [
            {
                "handoff_id": row["handoff_id"],
                "status": row["status"],
                "revision": row["revision"],
                "customer_ref": row["customer_id"],
                "summary_masked": row["summary_masked"],
                "ticket_id": row["ticket_id"],
                "assigned_to": row["assigned_to"],
            }
            for row in rows
        ]
        return self._page(items, offset, limit)

    def refunds(
        self,
        tenant_id: str,
        customer_ids: tuple[str, ...],
        status: str | None,
        customer_id: str | None,
        offset: int,
        limit: int,
    ) -> WorkbenchPage:
        clause, params = self._ids_clause(customer_ids)
        predicates = ["tenant_id=:tenant", clause]
        params.update({"tenant": tenant_id, "offset": offset, "limit": limit})
        if status is not None:
            predicates.append("review_status=:status")
            params["status"] = status
        if customer_id is not None:
            predicates.append("customer_id=:customer_filter")
            params["customer_filter"] = customer_id
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        "SELECT refund_request_id,customer_id,review_status AS status,amount_minor,"
                        "currency,review_revision AS revision "
                        "FROM refund_request WHERE "
                        + " AND ".join(predicates)
                        + " ORDER BY updated_at DESC,refund_request_id LIMIT :limit OFFSET :offset"
                    ),
                    params,
                )
                .mappings()
                .all()
            )
            funds = {}
            if rows:
                request_ids = [row["refund_request_id"] for row in rows]
                bind = {f"rid{i}": value for i, value in enumerate(request_ids)}
                funds = dict(
                    conn.execute(
                        text(
                            "SELECT refund_request_id,funds_status AS status FROM refund_execution "
                            "WHERE tenant_id=:tenant "
                            f"AND refund_request_id IN ({','.join(':' + key for key in bind)})"
                        ),
                        {"tenant": tenant_id, **bind},
                    ).all()
                )
        items = [
            {
                "refund_request_id": row["refund_request_id"],
                "customer_ref": row["customer_id"],
                "status": row["status"],
                "amount_minor": row["amount_minor"],
                "currency": row["currency"],
                "revision": row["revision"],
                "requires_manual_review": row["status"] == "reviewing",
                "funds_status": funds.get(row["refund_request_id"], "not_executed"),
            }
            for row in rows
        ]
        return self._page(items, offset, limit)


class MySQLWorkbenchTicketCommand:
    """Consume a staff confirmation and update the ticket in the same transaction."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def apply(
        self,
        detail: dict,
        *,
        auth: AuthContext,
        binding: OperationContext,
        confirmation_id: str,
        operation_id: str,
        idempotency_key: str,
    ):
        from domain.ticket.model import TicketStatus

        with self._engine.begin() as connection:
            repository = MySQLTicketRepository(self._engine, connection)
            request_digest = content_digest(
                {
                    "operation": binding.operation_type,
                    "target_id": binding.target_id,
                    "payload_digest": binding.payload_digest,
                    "expected_revision": binding.expected_revision,
                }
            )
            replay = repository.replay_result(
                auth.tenant_id, auth.subject_id, "update", idempotency_key, request_digest
            )
            if replay is not None:
                return replay
            stored = repository.get(auth.tenant_id, detail["ticket_id"], include_internal=True)
            if stored is None:
                raise ValueError("ticket unavailable")
            expected = binding.expected_revision
            if binding.operation_type == "assign":
                changed = stored.ticket.assign(detail["assigned_to"], expected_revision=expected)
            elif binding.operation_type == "status":
                changed = stored.ticket.change_status(
                    TicketStatus(detail["target_status"]), expected_revision=expected
                )
            else:
                raise ValueError("unsupported ticket operation")
            result = repository.save(
                changed,
                expected_revision=expected,
                actor_subject_id=auth.subject_id,
                context_snapshot=stored.context_snapshot,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
            )
            if not result.replayed:
                MySQLConfirmationStore(self._engine, connection).consume(
                    confirmation_id,
                    binding,
                    operation_id,
                    datetime.now(timezone.utc),
                )
            return result
