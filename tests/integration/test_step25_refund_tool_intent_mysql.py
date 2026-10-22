"""Refund intent sidecar end-to-end: MCP create resolves server-side state only.

The model-visible ``create_refund_request`` tool accepts order and reason only;
quote, confirmation and idempotency resolve from the registered refund intent.
Without a registered-and-verified confirmation the tool fails closed.
"""

import uuid
from datetime import datetime, timezone

import pytest
from bootstrap.container import MCP_TOOLS_PATH
from bootstrap.settings import load_settings
from sqlalchemy import text

from application.common.audit import AuditUseCase
from application.order.use_cases.order_query import QueryOrderUseCase, QueryPaymentUseCase
from application.refund.confirmation import RefundConfirmationUseCase
from application.refund.qualification import RefundQualificationUseCase
from application.refund.submission import RefundSubmissionUseCase
from infrastructure.auth.mock_auth_adapter import MockAuthAdapter
from infrastructure.messaging.event_contract import EventCatalog
from infrastructure.messaging.outbox_repository import OutboxRepository
from infrastructure.mock.order_adapter import MockOrderAdapter
from infrastructure.mock.refund_snapshot_adapter import MockRefundSnapshotAdapter
from infrastructure.observability.audit_repository import MySQLAuditRepository
from infrastructure.persistence.mysql.connection import create_mysql_engine
from infrastructure.persistence.mysql.repositories.refund_quote_repository import (
    MySqlRefundQuoteRepository,
)
from infrastructure.refund.mysql_tool_intent import MySqlRefundToolIntent
from infrastructure.refund.submission_adapter import MySqlMockRefundSubmissionAdapter
from infrastructure.security.confirmation_store import MySQLConfirmationStore
from interface.mcp.context_guard import ToolContextGuard
from interface.mcp.tools import MCPToolDeps, execute_tool_call

PROJECT_ROOT = "."
_CLEANUP_BY_ORDER = (
    "refund_request",
    "refund_order_guard",
)
# refund_tool_intent references both refund_quote and operation_confirmation;
# delete it before either parent, then the remaining parents.
_CLEANUP_AFTER_ORDER = ("refund_tool_intent", "refund_quote")


def _headers(token: str) -> dict:
    return {
        "authorization": f"Bearer {token}",
        "x-request-id": f"req-{uuid.uuid4().hex[:12]}",
        "x-correlation-id": f"corr-{uuid.uuid4().hex[:12]}",
        "x-trace-id": f"trace-{uuid.uuid4().hex[:12]}",
    }


@pytest.fixture
def refund_env():
    settings = load_settings()
    engine = create_mysql_engine(
        settings.mysql_url, settings.mysql_connect_timeout_s, settings.mysql_read_timeout_s
    )
    order_id = f"AT-RI-{uuid.uuid4().hex[:10]}"
    auth_gateway = MockAuthAdapter.from_fixture("data/fixtures/mock-identities.json", enabled=True)
    grant = auth_gateway.issue_mock_session("cust-north-01")
    catalog = EventCatalog("data/contracts/event-catalog.json", "data/contracts/events.schema.json")
    snapshots = MockRefundSnapshotAdapter(engine)
    quotes = MySqlRefundQuoteRepository(engine)
    deps = MCPToolDeps(
        auth_gateway=auth_gateway,
        guard=ToolContextGuard(MCP_TOOLS_PATH),
        query_order=QueryOrderUseCase(MockOrderAdapter(engine)),
        query_payment=QueryPaymentUseCase(MockOrderAdapter(engine)),
        audit=AuditUseCase(MySQLAuditRepository(engine)),
        refund_qualification=RefundQualificationUseCase(snapshots, quotes),
        refund_submission=RefundSubmissionUseCase(
            quotes,
            MySqlMockRefundSubmissionAdapter(engine, snapshots, OutboxRepository(engine, catalog)),
        ),
        refund_tool_intent=MySqlRefundToolIntent(engine),
    )
    try:
        yield engine, deps, grant, order_id
    finally:
        marker = f"%{order_id}%"
        with engine.begin() as connection:
            connection.execute(
                text(
                    "DELETE FROM consumer_inbox WHERE tenant_id='tenant-north' AND event_id IN"
                    " (SELECT event_id FROM outbox_event WHERE tenant_id='tenant-north'"
                    " AND payload LIKE :m)"
                ),
                {"m": marker},
            )
            for statement, params in (
                (
                    "DELETE FROM outbox_event WHERE tenant_id='tenant-north' AND payload LIKE :m",
                    {"m": marker},
                ),
                (
                    "DELETE FROM tool_invocation WHERE tenant_id='tenant-north'"
                    " AND args_summary LIKE :m",
                    {"m": marker},
                ),
                (
                    "DELETE FROM agent_audit_log WHERE tenant_id='tenant-north'"
                    " AND parameter_summary LIKE :m",
                    {"m": marker},
                ),
                (
                    "DELETE FROM refund_request_line WHERE tenant_id='tenant-north'"
                    " AND refund_request_id IN (SELECT refund_request_id FROM refund_request"
                    " WHERE tenant_id='tenant-north' AND order_id=:o)",
                    {"o": order_id},
                ),
                (
                    "DELETE FROM refund_quote_line WHERE tenant_id='tenant-north'"
                    " AND quote_id IN (SELECT quote_id FROM refund_quote"
                    " WHERE tenant_id='tenant-north' AND order_id=:o)",
                    {"o": order_id},
                ),
                (
                    "DELETE FROM operation_idempotency WHERE tenant_id='tenant-north'"
                    " AND actor_subject_id='customer-north-01' AND resource_id=:o",
                    {"o": order_id},
                ),
            ):
                connection.execute(text(statement), params)
            for table in _CLEANUP_BY_ORDER:
                connection.execute(
                    text(f"DELETE FROM {table} WHERE tenant_id='tenant-north' AND order_id=:o"),
                    {"o": order_id},
                )
            for table in _CLEANUP_AFTER_ORDER:
                connection.execute(
                    text(f"DELETE FROM {table} WHERE tenant_id='tenant-north' AND order_id=:o"),
                    {"o": order_id},
                )
            connection.execute(
                text(
                    "DELETE FROM operation_confirmation WHERE tenant_id='tenant-north'"
                    " AND operation_type='refund_request' AND target_id=:o"
                ),
                {"o": order_id},
            )
            for table in (
                "mock_pay_payment_detail",
                "mock_pay_payment",
                "mock_oms_order_line",
                "mock_oms_order",
            ):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE tenant_id='tenant-north' AND order_id=:o"),
                    {"o": order_id},
                )
        engine.dispose()


def _insert_paid_order(engine, order_id: str) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO mock_oms_order"
                " (tenant_id,order_id,customer_id,status,currency,paid_minor,version)"
                " VALUES ('tenant-north',:order,'CUSTOMER-01','pending_shipment','CNY',11000,1)"
            ),
            {"order": order_id},
        )
        connection.execute(
            text(
                "INSERT INTO mock_oms_order_line"
                " (tenant_id,order_id,line_id,source_product_id,"
                " quantity,goods_minor,shipping_minor)"
                " VALUES ('tenant-north',:order,'AT-RI-LINE','P-100',1,10000,1000)"
            ),
            {"order": order_id},
        )
        connection.execute(
            text(
                "INSERT INTO mock_pay_payment"
                " (tenant_id,payment_id,order_id,paid_minor,currency,status)"
                " VALUES ('tenant-north',:pay,:order,11000,'CNY','succeeded')"
            ),
            {"order": order_id, "pay": f"{order_id}-PAY"},
        )
        connection.execute(
            text(
                "INSERT INTO mock_pay_payment_detail"
                " (tenant_id,payment_detail_id,payment_id,order_id,line_id,product_id,"
                " paid_goods_minor,paid_shipping_minor,paid_minor,paid_at,detail_sequence)"
                " VALUES ('tenant-north',:detail,:pay,:order,'AT-RI-LINE','product-air-01',"
                " 10000,1000,11000,:paid_at,1)"
            ),
            {
                "order": order_id,
                "pay": f"{order_id}-PAY",
                "detail": f"{order_id}-DETAIL",
                "paid_at": now,
            },
        )


def _create_arguments(order_id: str) -> dict:
    return {"order_id": order_id, "reason_code": "changed_mind"}


def test_mcp_create_refund_resolves_registered_confirmation(refund_env) -> None:
    engine, deps, grant, order_id = refund_env
    _insert_paid_order(engine, order_id)
    token, auth = grant.session_token, grant.auth

    blind = execute_tool_call(
        "create_refund_request",
        _create_arguments(order_id),
        deps=deps,
        headers=_headers(token),
    )
    assert blind["status"] == "error"
    assert blind["error_code"] == "CONFIRMATION_INVALID"

    quote_response = execute_tool_call(
        "calculate_refund",
        {
            "order_id": order_id,
            "reason_code": "changed_mind",
            "lines": [{"order_line_id": "AT-RI-LINE", "quantity": 1}],
        },
        deps=deps,
        headers=_headers(token),
    )
    assert quote_response["status"] == "success"
    assert quote_response["data"]["amount_minor"] == 11000
    quote = deps.refund_submission.authorized_quote(auth, quote_response["data"]["quote_id"])

    flow = RefundConfirmationUseCase(deps.refund_submission, MySQLConfirmationStore(engine))
    prepared = flow.prepare(auth, quote.quote_id)
    deps.refund_tool_intent.register(auth, quote, prepared.record.confirmation_id)
    flow.verify(auth, prepared.record.confirmation_id, prepared.mock_challenge)

    created = execute_tool_call(
        "create_refund_request",
        _create_arguments(order_id),
        deps=deps,
        headers=_headers(token),
    )
    assert created["status"] == "success"
    assert created["data"]["funds_status"] == "not_executed"
    assert created["data"]["review_status"] == "pending_review"
    refund_request_id = created["data"]["refund_request_id"]

    replay = execute_tool_call(
        "create_refund_request",
        _create_arguments(order_id),
        deps=deps,
        headers=_headers(token),
    )
    assert replay["status"] == "success"
    assert replay["data"]["refund_request_id"] == refund_request_id


def test_mcp_create_refund_without_intent_stays_fail_closed(refund_env) -> None:
    engine, deps, grant, order_id = refund_env
    _insert_paid_order(engine, order_id)
    token = grant.session_token

    quote_response = execute_tool_call(
        "calculate_refund",
        {
            "order_id": order_id,
            "reason_code": "changed_mind",
            "lines": [{"order_line_id": "AT-RI-LINE", "quantity": 1}],
        },
        deps=deps,
        headers=_headers(token),
    )
    assert quote_response["status"] == "success"

    created = execute_tool_call(
        "create_refund_request",
        _create_arguments(order_id),
        deps=deps,
        headers=_headers(token),
    )
    assert created["status"] == "error"
    assert created["error_code"] == "CONFIRMATION_INVALID"
