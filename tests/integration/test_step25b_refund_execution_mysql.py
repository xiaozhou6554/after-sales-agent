"""步骤25B 集成验收：受控退款执行（FR-22）。

覆盖 TC-99 三阶段提交、TC-100 超时恢复不重付、TC-101 回调验签/重放/
主动查询、TC-102 部分分摊守恒、TC-103 版本/权限/金额/退货门、TC-109
simulation 标记与对账差异。渠道为独立 Mock 适配器（environment=simulation）。
"""

import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from bootstrap.settings import load_settings
from sqlalchemy import text

from application.common.audit import AuditUseCase
from application.common.authorization import AuthorizationDenied
from application.common.context import OperationContext, TraceContext
from application.common.operation_confirmation import (
    OperationConfirmationUseCase,
    content_digest,
)
from application.common.payment_errors import PaymentConflict
from application.order.use_cases.order_query import QueryOrderUseCase, QueryPaymentUseCase
from infrastructure.auth.mock_auth_adapter import MockAuthAdapter
from infrastructure.messaging.event_contract import EventCatalog
from infrastructure.messaging.outbox_repository import OutboxRepository
from infrastructure.mock.order_adapter import MockOrderAdapter
from infrastructure.observability.audit_repository import MySQLAuditRepository
from infrastructure.payment.mock_refund_channel import (
    MockRefundChannelAdapter,
    callback_signature,
)
from infrastructure.payment.mysql_refund_execution_service import (
    MySqlRefundExecutionService,
)
from infrastructure.persistence.mysql.connection import create_mysql_engine
from infrastructure.security.confirmation_store import MySQLConfirmationStore
from interface.mcp.context_guard import ToolContextGuard
from interface.mcp.tools import MCPToolDeps, execute_tool_call

_TENANT = "tenant-north"
_POLICY_VERSION = "2026-10-v1"
_PAYMENT_VERSION = "mock-v2"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class _Env:
    """25B 测试环境：服务+渠道+身份；按订单标记清理全部衍生数据。"""

    def __init__(self, engine) -> None:
        self.engine = engine
        self.channel = MockRefundChannelAdapter(engine)
        self.service = MySqlRefundExecutionService(
            engine,
            self.channel,
            OutboxRepository(
                engine,
                EventCatalog("data/contracts/event-catalog.json",
                             "data/contracts/events.schema.json"),
            ),
        )
        gateway = MockAuthAdapter.from_fixture(
            "data/fixtures/mock-identities.json", enabled=True
        )
        self.pay = gateway.issue_mock_session("svc-pay-north-01")
        self.wh = gateway.issue_mock_session("wh-north-01")
        self.cust = gateway.issue_mock_session("cust-north-01")
        self.orders: list[str] = []

    def trace(self, order_id: str, tag: str) -> TraceContext:
        run = uuid.uuid4().hex[:8]
        return TraceContext(
            "1.0", f"req-{order_id}-{tag}-{run}",
            f"corr-{order_id}-{tag}", f"trace-{order_id}-{tag}", None,
        )

    def seed(
        self,
        lines: list[tuple[str, int, int, int]],
        *,
        request_amount: int | None = None,
        reason_code: str = "quality_issue",
        policy_version: str = _POLICY_VERSION,
        payment_version: str = _PAYMENT_VERSION,
        completed_return: bool = False,
    ):
        """lines: (line_id, qty, paid_goods, paid_shipping)；默认按全额批准。"""
        order_id = f"AT-PAY-{uuid.uuid4().hex[:10]}"
        self.orders.append(order_id)
        refund_request_id = f"RR-{order_id}"
        payment_id = f"PAY-{order_id}"
        paid_total = sum(goods + shipping for _, _, goods, shipping in lines)
        approved = paid_total if request_amount is None else request_amount
        confirmation_id = self._consumed_confirmation(order_id, refund_request_id)
        with self.engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO mock_oms_order"
                " (tenant_id,order_id,customer_id,status,currency,paid_minor,version)"
                " VALUES (:t,:o,'CUSTOMER-01','pending_shipment','CNY',:paid,1)"
            ), {"t": _TENANT, "o": order_id, "paid": paid_total})
            for line_id, qty, goods, shipping in lines:
                connection.execute(text(
                    "INSERT INTO mock_oms_order_line"
                    " (tenant_id,order_id,line_id,source_product_id,quantity,"
                    " goods_minor,shipping_minor)"
                    " VALUES (:t,:o,:line,'product-air-01',:qty,:goods,:shipping)"
                ), {"t": _TENANT, "o": order_id, "line": line_id,
                    "qty": qty, "goods": goods, "shipping": shipping})
            connection.execute(text(
                "INSERT INTO mock_pay_payment"
                " (tenant_id,payment_id,order_id,paid_minor,currency,status,"
                " source_record_id)"
                " VALUES (:t,:p,:o,:paid,'CNY','succeeded',:src)"
            ), {"t": _TENANT, "p": payment_id, "o": order_id,
                "paid": paid_total, "src": f"SRC-{payment_id}"})
            for sequence, (line_id, qty, goods, shipping) in enumerate(lines, start=1):
                connection.execute(text(
                    "INSERT INTO mock_pay_payment_detail"
                    " (tenant_id,payment_detail_id,payment_id,order_id,line_id,"
                    " product_id,paid_goods_minor,paid_shipping_minor,paid_minor,"
                    " paid_at,detail_sequence)"
                    " VALUES (:t,:detail,:p,:o,:line,'product-air-01',:goods,:shipping,"
                    " :paid,NOW(6),:seq)"
                ), {"t": _TENANT, "detail": f"PD-{order_id}-{line_id}",
                    "p": payment_id, "o": order_id, "line": line_id,
                    "goods": goods, "shipping": shipping,
                    "paid": goods + shipping, "seq": sequence})
            approved_goods = sum(line[2] for line in lines)
            approved_shipping = sum(line[3] for line in lines)
            connection.execute(text(
                "INSERT INTO refund_request"
                " (tenant_id,refund_request_id,quote_id,order_id,actor_subject_id,"
                " customer_id,confirmation_id,idempotency_key,reason_code,eligibility,"
                " review_status,amount_minor,goods_minor,shipping_minor,currency,"
                " policy_id,policy_version,order_version,payment_version,"
                " created_at,updated_at)"
                " VALUES (:t,:rid,:quote,:o,'customer-north-01','CUSTOMER-01',"
                " :conf,:idem,:reason,'eligible','approved',:amount,:goods,"
                " :shipping,'CNY','MOCK-FR-07.1',:pv,'1',:payv,NOW(6),NOW(6))"
            ), {"t": _TENANT, "rid": refund_request_id, "quote": f"Q-{order_id}",
                "o": order_id, "conf": confirmation_id, "idem": f"I-{order_id}",
                "reason": reason_code, "amount": approved,
                "goods": approved_goods, "shipping": approved_shipping,
                "pv": policy_version, "payv": payment_version})
            for line_id, qty, goods, shipping in lines:
                connection.execute(text(
                    "INSERT INTO refund_request_line"
                    " (tenant_id,refund_request_id,line_id,quantity,goods_minor,"
                    " shipping_minor)"
                    " VALUES (:t,:rid,:line,:qty,:goods,:shipping)"
                ), {"t": _TENANT, "rid": refund_request_id, "line": line_id,
                    "qty": qty, "goods": goods, "shipping": shipping})
            if completed_return:
                self._insert_completed_return(connection, order_id, refund_request_id, lines)
        return SimpleNamespace(
            order_id=order_id, refund_request_id=refund_request_id,
            payment_id=payment_id, paid_total=paid_total, approved=approved,
        )

    @staticmethod
    def _insert_completed_return(connection, order_id, refund_request_id, lines) -> None:
        return_id = f"RT-{order_id}"
        connection.execute(text(
            "INSERT INTO return_order"
            " (tenant_id,return_id,refund_request_id,order_id,customer_id,created_by,"
            " status,revision,environment,created_at,updated_at)"
            " VALUES (:t,:r,:rid,:o,'CUSTOMER-01','warehouse-north-01','completed',4,"
            " 'simulation',NOW(6),NOW(6))"
        ), {"t": _TENANT, "r": return_id, "rid": refund_request_id, "o": order_id})
        for line_id, qty, _goods, _shipping in lines:
            connection.execute(text(
                "INSERT INTO return_line"
                " (tenant_id,return_id,line_id,order_line_id,rma_id,approved_qty,"
                " received_qty,accepted_qty,rejected_qty)"
                " VALUES (:t,:r,:line,:line,:rma,:qty,:qty,:qty,0)"
            ), {"t": _TENANT, "r": return_id, "line": line_id,
                "rma": f"RMA-{order_id}-{line_id}", "qty": qty})

    def _consumed_confirmation(self, order_id: str, refund_request_id: str) -> str:
        store = MySQLConfirmationStore(self.engine)
        use_case = OperationConfirmationUseCase(store, 300)
        binding = OperationContext.from_trusted_actor(
            self.cust.auth,
            customer_id="CUSTOMER-01",
            confirmer_subject_id=self.cust.auth.subject_id,
            operation_type="refund_request",
            target_id=order_id,
            payload_digest=content_digest({"order_id": order_id}),
            expected_revision=None,
        )
        prepared = use_case.prepare(binding)
        use_case.verify(_TENANT, prepared.record.confirmation_id, prepared.mock_challenge)
        store.consume(
            prepared.record.confirmation_id, binding,
            operation_id=f"seed-{refund_request_id}", now=datetime.now(timezone.utc),
        )
        return prepared.record.confirmation_id

    def cleanup(self) -> None:
        with self.engine.begin() as connection:
            for order_id in self.orders:
                marker = f"%{order_id}%"
                rid_sub = (
                    "SELECT refund_request_id FROM refund_request"
                    f" WHERE tenant_id='{_TENANT}' AND order_id=:o"
                )
                exec_sub = (
                    "SELECT execution_id FROM refund_execution"
                    f" WHERE tenant_id='{_TENANT}'"
                    f" AND refund_request_id IN ({rid_sub})"
                )
                statements = (
                    ("DELETE FROM consumer_inbox WHERE tenant_id=:t AND event_id IN"
                     " (SELECT event_id FROM outbox_event WHERE tenant_id=:t"
                     " AND payload LIKE :m)", {"t": _TENANT, "m": marker}),
                    ("DELETE FROM outbox_event WHERE tenant_id=:t AND payload LIKE :m",
                     {"t": _TENANT, "m": marker}),
                    ("DELETE FROM agent_audit_log WHERE tenant_id=:t AND"
                     " (request_id LIKE :rm OR parameter_summary LIKE :m)",
                     {"t": _TENANT, "m": marker, "rm": f"req-{order_id}-%"}),
                    ("DELETE FROM reconciliation_difference WHERE tenant_id=:t AND"
                     " (detail LIKE :m OR channel_ref IN"
                     f" (SELECT refund_id FROM mock_pay_refund_execution"
                     f"  WHERE tenant_id='{_TENANT}' AND order_id=:o))",
                     {"t": _TENANT, "m": marker, "o": order_id}),
                    ("DELETE FROM reconciliation_run WHERE tenant_id=:t"
                     " AND merchant_id=:mer",
                     {"t": _TENANT, "mer": f"mer-{order_id}"}),
                    (f"DELETE FROM refund_ledger WHERE tenant_id=:t"
                     f" AND execution_id IN ({exec_sub})", {"t": _TENANT, "o": order_id}),
                    (f"DELETE FROM refund_allocation WHERE tenant_id=:t"
                     f" AND execution_id IN ({exec_sub})", {"t": _TENANT, "o": order_id}),
                    (f"DELETE FROM refund_execution WHERE tenant_id=:t"
                     f" AND refund_request_id IN ({rid_sub})", {"t": _TENANT, "o": order_id}),
                    ("DELETE FROM operation_confirmation WHERE tenant_id=:t"
                     " AND operation_type='refund_request' AND target_id=:o",
                     {"t": _TENANT, "o": order_id}),
                    ("DELETE FROM operation_idempotency WHERE tenant_id=:t"
                     " AND idempotency_key LIKE :m", {"t": _TENANT, "m": marker}),
                    (f"DELETE FROM return_line WHERE tenant_id=:t AND return_id IN"
                     f" (SELECT return_id FROM return_order WHERE tenant_id='{_TENANT}'"
                     f" AND refund_request_id IN ({rid_sub}))", {"t": _TENANT, "o": order_id}),
                    (f"DELETE FROM return_order WHERE tenant_id=:t"
                     f" AND refund_request_id IN ({rid_sub})", {"t": _TENANT, "o": order_id}),
                    ("DELETE FROM mock_pay_refund_detail WHERE tenant_id=:t AND refund_id IN"
                     f" (SELECT refund_id FROM mock_pay_refund_execution"
                     f"  WHERE tenant_id='{_TENANT}' AND order_id=:o)",
                     {"t": _TENANT, "o": order_id}),
                    ("DELETE FROM mock_pay_refund_execution WHERE tenant_id=:t"
                     " AND order_id=:o", {"t": _TENANT, "o": order_id}),
                    (f"DELETE FROM refund_request_line WHERE tenant_id=:t"
                     f" AND refund_request_id IN ({rid_sub})", {"t": _TENANT, "o": order_id}),
                    ("DELETE FROM refund_request WHERE tenant_id=:t AND order_id=:o",
                     {"t": _TENANT, "o": order_id}),
                    ("DELETE FROM mock_pay_payment_detail WHERE tenant_id=:t AND order_id=:o",
                     {"t": _TENANT, "o": order_id}),
                    ("DELETE FROM mock_pay_payment WHERE tenant_id=:t AND order_id=:o",
                     {"t": _TENANT, "o": order_id}),
                    ("DELETE FROM mock_oms_order_line WHERE tenant_id=:t AND order_id=:o",
                     {"t": _TENANT, "o": order_id}),
                    ("DELETE FROM mock_oms_order WHERE tenant_id=:t AND order_id=:o",
                     {"t": _TENANT, "o": order_id}),
                )
                for statement, params in statements:
                    connection.execute(text(statement), params)


@pytest.fixture
def pay_env():
    settings = load_settings()
    engine = create_mysql_engine(
        settings.mysql_url, settings.mysql_connect_timeout_s, settings.mysql_read_timeout_s
    )
    env = _Env(engine)
    try:
        yield env
    finally:
        env.cleanup()
        engine.dispose()


def _allocation(line_id: str, component: str, amount_minor: int) -> dict:
    return {"request_line_id": line_id, "component": component,
            "amount_minor": amount_minor}


def _db_one(engine, sql: str, params: dict):
    with engine.connect() as connection:
        return connection.execute(text(sql), params).scalar()


def test_tc99_submit_three_stage_ledger_reservation_and_replay(pay_env) -> None:
    env = pay_env
    order = env.seed([("L1", 1, 10000, 1000)])
    service, auth = env.service, env.pay.auth
    assert service.query_by_request(env.cust.auth, order.refund_request_id)[
        "funds_status"] == "not_executed"

    result = service.submit(
        auth, order.refund_request_id,
        [_allocation("L1", "goods", 10000), _allocation("L1", "shipping", 1000)],
        idempotency_key=f"{order.order_id}-X1",
        trace=env.trace(order.order_id, "submit"),
    )
    assert result["replayed"] is False
    assert result["funds_status"] == "succeeded"
    assert result["environment"] == "simulation"
    assert result["channel_ref"] and result["channel_transaction_ref"]
    execution_id = result["execution_id"]

    # 三阶段证据：执行行 + 账本唯一 + 渠道事实（含明细）。
    with env.engine.connect() as connection:
        execution = connection.execute(text(
            "SELECT funds_status,channel_ref,channel_transaction_ref,environment"
            " FROM refund_execution WHERE tenant_id=:t AND execution_id=:e"
        ), {"t": _TENANT, "e": execution_id}).mappings().first()
        ledger = connection.execute(text(
            "SELECT channel_transaction_id,amount_minor,evidence_ref,environment"
            " FROM refund_ledger WHERE tenant_id=:t AND execution_id=:e"
        ), {"t": _TENANT, "e": execution_id}).mappings().all()
        channel_row = connection.execute(text(
            "SELECT status,environment,created_at FROM mock_pay_refund_execution"
            " WHERE tenant_id=:t AND order_id=:o"
        ), {"t": _TENANT, "o": order.order_id}).mappings().all()
        channel_details = connection.execute(text(
            "SELECT payment_detail_id,status,refunded_goods_minor"
            " FROM mock_pay_refund_detail WHERE tenant_id=:t AND refund_id=:r"
        ), {"t": _TENANT, "r": result["channel_ref"]}).mappings().all()
        events = connection.execute(text(
            "SELECT event_type,payload FROM outbox_event"
            " WHERE tenant_id=:t AND payload LIKE :m"
        ), {"t": _TENANT, "m": f"%{execution_id}%"}).all()
    assert execution["funds_status"] == "succeeded"
    assert execution["environment"] == "simulation"
    assert len(ledger) == 1
    assert ledger[0]["channel_transaction_id"] == result["channel_transaction_ref"]
    assert int(ledger[0]["amount_minor"]) == 11000
    assert ledger[0]["evidence_ref"] == result["channel_ref"]
    assert len(channel_row) == 1 and channel_row[0]["status"] == "succeeded"
    assert channel_row[0]["environment"] == "simulation"
    assert channel_row[0]["created_at"] is not None
    assert {row["payment_detail_id"] for row in channel_details} == {
        f"PD-{order.order_id}-L1"}
    event_types = {row[0] for row in events}
    assert {"refund.execution.requested", "refund.execution.succeeded"} <= event_types
    producers = {}
    for event_type, payload in events:
        envelope = json.loads(payload)
        producers[event_type] = envelope.get("producer")
        assert envelope["payload"].get("environment") == "simulation"
    assert producers["refund.execution.requested"] == "refund-coordinator"
    assert producers["refund.execution.succeeded"] == "payment-adapter"

    # 预留结转：成功后 succeeded=批准额、reserved=0。
    progress = service.query_by_request(env.cust.auth, order.refund_request_id)
    assert progress["funds_status"] == "succeeded"
    assert progress["succeeded_minor"] == 11000
    assert progress["reserved_minor"] == 0

    # 同幂等键重放：同一执行，不新增账本。
    replay = service.submit(
        auth, order.refund_request_id,
        [_allocation("L1", "goods", 10000), _allocation("L1", "shipping", 1000)],
        idempotency_key=f"{order.order_id}-X1",
        trace=env.trace(order.order_id, "replay"),
    )
    assert replay["replayed"] is True
    assert replay["execution_id"] == execution_id
    assert _db_one(env.engine,
                   "SELECT COUNT(*) FROM refund_ledger WHERE tenant_id=:t"
                   " AND execution_id=:e", {"t": _TENANT, "e": execution_id}) == 1

    # 模型可见只读工具：客户经 MCP 查执行进度。
    deps = MCPToolDeps(
        auth_gateway=MockAuthAdapter.from_fixture(
            "data/fixtures/mock-identities.json", enabled=True
        ),
        guard=ToolContextGuard("data/contracts/mcp-tools.json"),
        query_order=QueryOrderUseCase(MockOrderAdapter(env.engine)),
        query_payment=QueryPaymentUseCase(MockOrderAdapter(env.engine)),
        audit=AuditUseCase(MySQLAuditRepository(env.engine)),
        refund_execution_query=service,
    )
    headers = {"authorization": f"Bearer {env.cust.session_token}"}
    tool_result = execute_tool_call(
        "query_refund_execution",
        {"refund_request_id": order.refund_request_id},
        deps=deps, headers=headers,
    )
    assert tool_result["status"] == "success"
    assert tool_result["data"]["funds_status"] == "succeeded"
    assert tool_result["data"]["executions"][0]["environment"] == "simulation"


def test_tc100_timeout_unknown_recovery_without_double_payment(pay_env) -> None:
    env = pay_env
    order = env.seed([("L1", 1, 10000, 1000)])
    service, channel, auth = env.service, env.channel, env.pay.auth
    idem = f"{order.order_id}-T1"
    channel.set_script(idem, "timeout")

    result = service.submit(
        auth, order.refund_request_id,
        [_allocation("L1", "goods", 10000), _allocation("L1", "shipping", 1000)],
        idempotency_key=idem, trace=env.trace(order.order_id, "timeout"),
    )
    execution_id = result["execution_id"]
    assert result["funds_status"] == "unknown"
    # 超时不是失败：预留保留、无渠道事实、无账本。
    progress = service.query_by_request(env.cust.auth, order.refund_request_id)
    assert progress["funds_status"] == "unknown"
    assert progress["reserved_minor"] == 11000
    assert _db_one(env.engine,
                   "SELECT COUNT(*) FROM mock_pay_refund_execution"
                   " WHERE tenant_id=:t AND order_id=:o",
                   {"t": _TENANT, "o": order.order_id}) == 0

    # 渠道仍无记录时恢复：只查询，维持 unknown。
    recovered = service.recover(auth, execution_id,
                                trace=env.trace(order.order_id, "recover-1"))
    assert recovered["funds_status"] == "unknown"

    # 渠道侧最终成功（模拟商户门户补记）：稳定执行 ID 查询到权威证据。
    channel.execute(
        _TENANT, execution_id, order.order_id, order.payment_id,
        [
            {"payment_detail_id": f"PD-{order.order_id}-L1",
             "component": "goods", "amount_minor": 10000},
            {"payment_detail_id": f"PD-{order.order_id}-L1",
             "component": "shipping", "amount_minor": 1000},
        ],
        11000, "CNY", f"{order.order_id}-PORTAL",
    )
    recovered = service.recover(auth, execution_id,
                                trace=env.trace(order.order_id, "recover-2"))
    assert recovered["funds_status"] == "succeeded"
    assert _db_one(env.engine,
                   "SELECT COUNT(*) FROM refund_ledger WHERE tenant_id=:t"
                   " AND execution_id=:e", {"t": _TENANT, "e": execution_id}) == 1

    # 同幂等键再次提交：重放原执行，不重付。
    again = service.submit(
        auth, order.refund_request_id,
        [_allocation("L1", "goods", 10000), _allocation("L1", "shipping", 1000)],
        idempotency_key=idem, trace=env.trace(order.order_id, "resubmit"),
    )
    assert again["replayed"] is True
    assert again["execution_id"] == execution_id
    assert _db_one(env.engine,
                   "SELECT COUNT(*) FROM mock_pay_refund_execution"
                   " WHERE tenant_id=:t AND order_id=:o",
                   {"t": _TENANT, "o": order.order_id}) == 1

    # 事件各恰好一次（requested/succeeded 不重复）。
    with env.engine.connect() as connection:
        counts = connection.execute(text(
            "SELECT event_type, COUNT(*) FROM outbox_event"
            " WHERE tenant_id=:t AND payload LIKE :m GROUP BY event_type"
        ), {"t": _TENANT, "m": f"%{execution_id}%"}).all()
    by_type = {row[0]: row[1] for row in counts}
    assert by_type.get("refund.execution.requested") == 1
    assert by_type.get("refund.execution.succeeded") == 1


def test_tc101_callback_signature_replay_and_mismatch(pay_env) -> None:
    env = pay_env
    order = env.seed([("L1", 1, 10000, 1000)])
    service, auth = env.service, env.pay.auth
    result = service.submit(
        auth, order.refund_request_id,
        [_allocation("L1", "goods", 10000), _allocation("L1", "shipping", 1000)],
        idempotency_key=f"{order.order_id}-C1",
        trace=env.trace(order.order_id, "submit"),
    )
    refund_id = result["channel_ref"]
    txn = result["channel_transaction_ref"]
    payload = {
        "tenant_id": _TENANT, "refund_id": refund_id, "order_id": order.order_id,
        "amount_minor": 11000, "currency": "CNY", "status": "succeeded",
    }

    # 伪造签名：拒绝并记录差异；已成功的执行不回退。
    forged = service.handle_callback(
        payload, "f" * 64, trace=env.trace(order.order_id, "cb-forged")
    )
    assert forged == {"accepted": False, "reason": "invalid_signature"}
    assert _db_one(env.engine,
                   "SELECT funds_status FROM refund_execution WHERE tenant_id=:t"
                   " AND execution_id=:e",
                   {"t": _TENANT, "e": result["execution_id"]}) == "succeeded"
    assert _db_one(env.engine,
                   "SELECT COUNT(*) FROM reconciliation_difference WHERE tenant_id=:t"
                   " AND detail LIKE :m",
                   {"t": _TENANT, "m": f"%{order.order_id}%"}) == 1

    # 正式签名回调：触发权威查询；执行已成功 → 无操作且成功不回退。
    accepted = service.handle_callback(
        payload, callback_signature(payload),
        trace=env.trace(order.order_id, "cb-ok"),
    )
    assert accepted == {"accepted": True, "execution_id": result["execution_id"],
                        "noop": True}
    replay = service.handle_callback(
        payload, callback_signature(payload),
        trace=env.trace(order.order_id, "cb-replay"),
    )
    assert replay["noop"] is True
    assert _db_one(env.engine,
                   "SELECT COUNT(*) FROM refund_ledger WHERE tenant_id=:t"
                   " AND execution_id=:e",
                   {"t": _TENANT, "e": result["execution_id"]}) == 1

    # 金额不符（签名合法）：拒绝、记差异、状态不变。
    mismatch_payload = {**payload, "amount_minor": 999}
    mismatch = service.handle_callback(
        mismatch_payload, callback_signature(mismatch_payload),
        trace=env.trace(order.order_id, "cb-mismatch"),
    )
    assert mismatch["accepted"] is False and mismatch["reason"] == "mismatch"
    assert _db_one(env.engine,
                   "SELECT COUNT(*) FROM reconciliation_difference WHERE tenant_id=:t"
                   " AND difference_type='callback_mismatch' AND amount_minor=999",
                   {"t": _TENANT}) == 1

    # 未知退款单：拒绝并记录。
    unknown_payload = {**payload, "refund_id": f"unknown-{order.order_id}"}
    unknown = service.handle_callback(
        unknown_payload, callback_signature(unknown_payload),
        trace=env.trace(order.order_id, "cb-unknown"),
    )
    assert unknown == {"accepted": False, "reason": "unknown_refund"}

    # 账本引用的渠道流水保持唯一且未变。
    assert _db_one(env.engine,
                   "SELECT channel_transaction_id FROM refund_ledger WHERE tenant_id=:t"
                   " AND execution_id=:e",
                   {"t": _TENANT, "e": result["execution_id"]}) == txn


def test_tc101b_callback_without_authoritative_evidence_keeps_unknown(pay_env) -> None:
    env = pay_env
    order = env.seed([("L1", 1, 10000, 1000)])
    service, channel, auth = env.service, env.channel, env.pay.auth
    idem = f"{order.order_id}-U1"
    channel.set_script(idem, "timeout")
    result = service.submit(
        auth, order.refund_request_id,
        [_allocation("L1", "goods", 10000), _allocation("L1", "shipping", 1000)],
        idempotency_key=idem, trace=env.trace(order.order_id, "submit"),
    )
    assert result["funds_status"] == "unknown"
    # 渠道无记录；unknown 执行没有 channel_ref，回调伪造渠道退款单号声称成功。
    payload = {
        "tenant_id": _TENANT, "refund_id": "fabricated-refund-id",
        "order_id": order.order_id, "amount_minor": 11000,
        "currency": "CNY", "status": "succeeded",
    }
    outcome = service.handle_callback(
        payload, callback_signature(payload),
        trace=env.trace(order.order_id, "cb-noevidence"),
    )
    assert outcome["accepted"] is False
    assert outcome["reason"] in ("unknown_refund", "no_authoritative_success")
    assert _db_one(env.engine,
                   "SELECT funds_status FROM refund_execution WHERE tenant_id=:t"
                   " AND execution_id=:e",
                   {"t": _TENANT, "e": result["execution_id"]}) == "unknown"


def test_tc102_partial_allocations_conservation(pay_env) -> None:
    env = pay_env
    order = env.seed([("L1", 1, 8000, 200), ("L2", 1, 2000, 800)])
    service, auth = env.service, env.pay.auth
    trace = env.trace(order.order_id, "partial")

    first = service.submit(auth, order.refund_request_id,
                           [_allocation("L1", "goods", 5000)],
                           idempotency_key=f"{order.order_id}-P1", trace=trace)
    assert first["funds_status"] == "succeeded"
    second = service.submit(
        auth, order.refund_request_id,
        [_allocation("L1", "goods", 3000), _allocation("L2", "shipping", 800)],
        idempotency_key=f"{order.order_id}-P2", trace=trace,
    )
    assert second["funds_status"] == "succeeded"

    # 超出该明细实付：L1 商品已用满 8000。
    with pytest.raises(PaymentConflict, match="分摊累计超过"):
        service.submit(auth, order.refund_request_id,
                       [_allocation("L1", "goods", 1)],
                       idempotency_key=f"{order.order_id}-P3", trace=trace)
    # 超出该明细运费实付：L1 运费仅 200。
    with pytest.raises(PaymentConflict, match="分摊累计超过"):
        service.submit(auth, order.refund_request_id,
                       [_allocation("L1", "shipping", 300)],
                       idempotency_key=f"{order.order_id}-P4", trace=trace)

    third = service.submit(auth, order.refund_request_id,
                           [_allocation("L2", "goods", 2000)],
                           idempotency_key=f"{order.order_id}-P5", trace=trace)
    fourth = service.submit(auth, order.refund_request_id,
                            [_allocation("L1", "shipping", 200)],
                            idempotency_key=f"{order.order_id}-P6", trace=trace)
    assert third["funds_status"] == fourth["funds_status"] == "succeeded"

    progress = service.query_by_request(env.cust.auth, order.refund_request_id)
    assert progress["succeeded_minor"] == 11000
    assert progress["funds_status"] == "succeeded"
    assert len(progress["executions"]) == 4
    # 账本渠道流水唯一：无重复引用。
    with env.engine.connect() as connection:
        ledger_rows = connection.execute(text(
            "SELECT channel_transaction_id, COUNT(*) AS c FROM refund_ledger"
            " WHERE tenant_id=:t GROUP BY channel_transaction_id HAVING c > 1"
        ), {"t": _TENANT}).all()
        allocations = connection.execute(text(
            "SELECT a.payment_detail_id,a.component,SUM(a.amount_minor) AS total"
            " FROM refund_allocation a JOIN refund_execution e"
            " ON e.tenant_id=a.tenant_id AND e.execution_id=a.execution_id"
            " WHERE a.tenant_id=:t AND e.funds_status='succeeded'"
            " GROUP BY a.payment_detail_id,a.component"
        ), {"t": _TENANT}).all()
    assert ledger_rows == []
    reserved_by_key = {(row[0], row[1]): int(row[2]) for row in allocations}
    assert reserved_by_key[(f"PD-{order.order_id}-L1", "goods")] == 8000
    assert reserved_by_key[(f"PD-{order.order_id}-L1", "shipping")] == 200
    assert reserved_by_key[(f"PD-{order.order_id}-L2", "goods")] == 2000
    assert reserved_by_key[(f"PD-{order.order_id}-L2", "shipping")] == 800


def test_tc103_version_role_amount_and_return_gates(pay_env) -> None:
    env = pay_env
    order = env.seed([("L1", 1, 10000, 1000)])
    service, auth = env.service, env.pay.auth
    rid = order.refund_request_id
    trace = env.trace(order.order_id, "gates")
    full = [_allocation("L1", "goods", 10000), _allocation("L1", "shipping", 1000)]

    # 角色/范围门：客户与仓库身份都不得执行退款。
    with pytest.raises(AuthorizationDenied):
        service.submit(env.cust.auth, rid, full,
                       idempotency_key=f"{order.order_id}-G0", trace=trace)
    with pytest.raises(AuthorizationDenied):
        service.submit(env.wh.auth, rid, full,
                       idempotency_key=f"{order.order_id}-G0b", trace=trace)

    def _update(column: str, value: str) -> None:
        with env.engine.begin() as connection:
            connection.execute(text(
                f"UPDATE refund_request SET {column}=:v WHERE tenant_id=:t"
                " AND refund_request_id=:r"
            ), {"v": value, "t": _TENANT, "r": rid})

    # TC-103：批准版本过期（政策/支付）必须重新确认审核。
    _update("policy_version", "2020-01-old")
    with pytest.raises(PaymentConflict, match="批准版本已过期"):
        service.submit(auth, rid, full,
                       idempotency_key=f"{order.order_id}-G1", trace=trace)
    _update("policy_version", _POLICY_VERSION)
    _update("payment_version", "mock-v1")
    with pytest.raises(PaymentConflict, match="批准版本已过期"):
        service.submit(auth, rid, full,
                       idempotency_key=f"{order.order_id}-G2", trace=trace)
    _update("payment_version", _PAYMENT_VERSION)

    # TC-103：累计退款不得超过批准金额。
    with env.engine.begin() as connection:
        connection.execute(text(
            "UPDATE refund_request SET amount_minor=5000, goods_minor=4000,"
            " shipping_minor=1000 WHERE tenant_id=:t AND refund_request_id=:r"
        ), {"t": _TENANT, "r": rid})
    with pytest.raises(PaymentConflict, match="累计退款超过批准金额"):
        service.submit(auth, rid, full,
                       idempotency_key=f"{order.order_id}-G3", trace=trace)
    with env.engine.begin() as connection:
        connection.execute(text(
            "UPDATE refund_request SET amount_minor=11000, goods_minor=10000,"
            " shipping_minor=1000 WHERE tenant_id=:t AND refund_request_id=:r"
        ), {"t": _TENANT, "r": rid})

    # 分摊行不得超出申请范围。
    with pytest.raises(PaymentConflict, match="超出退款申请范围"):
        service.submit(auth, rid, [_allocation("LX", "goods", 100)],
                       idempotency_key=f"{order.order_id}-G4", trace=trace)

    # 门全部通过后执行成功，且以上拒绝均未留下执行记录。
    ok = service.submit(auth, rid, full,
                        idempotency_key=f"{order.order_id}-G5", trace=trace)
    assert ok["funds_status"] == "succeeded"
    assert _db_one(env.engine,
                   "SELECT COUNT(*) FROM refund_execution WHERE tenant_id=:t"
                   " AND refund_request_id=:r", {"t": _TENANT, "r": rid}) == 1


def test_tc103b_necessary_return_gate_blocks_then_allows(pay_env) -> None:
    env = pay_env
    order = env.seed([("L1", 1, 10000, 1000)], reason_code="no_longer_needed")
    service, auth = env.service, env.pay.auth
    rid = order.refund_request_id
    with pytest.raises(PaymentConflict, match="必要退货验收未完成"):
        service.submit(auth, rid,
                       [_allocation("L1", "goods", 10000),
                        _allocation("L1", "shipping", 1000)],
                       idempotency_key=f"{order.order_id}-R1",
                       trace=env.trace(order.order_id, "gate"))
    with env.engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO return_order"
            " (tenant_id,return_id,refund_request_id,order_id,customer_id,created_by,"
            " status,revision,environment,created_at,updated_at)"
            " VALUES (:t,:r,:rid,:o,'CUSTOMER-01','warehouse-north-01','completed',4,"
            " 'simulation',NOW(6),NOW(6))"
        ), {"t": _TENANT, "r": f"RT-{order.order_id}", "rid": rid, "o": order.order_id})
        connection.execute(text(
            "INSERT INTO return_line"
            " (tenant_id,return_id,line_id,order_line_id,rma_id,approved_qty,"
            " received_qty,accepted_qty,rejected_qty)"
            " VALUES (:t,:r,'L1','L1',:rma,1,1,1,0)"
        ), {"t": _TENANT, "r": f"RT-{order.order_id}",
            "rma": f"RMA-{order.order_id}-L1"})
    done = service.submit(auth, rid,
                          [_allocation("L1", "goods", 10000),
                           _allocation("L1", "shipping", 1000)],
                          idempotency_key=f"{order.order_id}-R2",
                          trace=env.trace(order.order_id, "gate-ok"))
    assert done["funds_status"] == "succeeded"


def test_reconciliation_balanced_channel_only_and_replay(pay_env) -> None:
    env = pay_env
    order = env.seed([("L1", 1, 10000, 1000)])
    service, channel, auth = env.service, env.channel, env.pay.auth
    service.submit(
        auth, order.refund_request_id,
        [_allocation("L1", "goods", 10000), _allocation("L1", "shipping", 1000)],
        idempotency_key=f"{order.order_id}-RC",
        trace=env.trace(order.order_id, "submit"),
    )
    window_start = _now() - timedelta(hours=1)
    window_end = _now() + timedelta(hours=1)
    merchant = f"mer-{order.order_id}"

    run = service.reconcile(auth, merchant_id=merchant, currency="CNY",
                            window_start=window_start, window_end=window_end,
                            trace=env.trace(order.order_id, "rec-1"))
    assert run["status"] == "balanced"
    assert run["difference_count"] == 0 and run["replayed"] is False
    replay = service.reconcile(auth, merchant_id=merchant, currency="CNY",
                               window_start=window_start, window_end=window_end,
                               trace=env.trace(order.order_id, "rec-1b"))
    assert replay["replayed"] is True and replay["run_id"] == run["run_id"]

    # 渠道单边流水（无账本证据）→ difference，且不自动冲销。
    channel.execute(
        _TENANT, uuid.uuid4().hex, order.order_id, order.payment_id,
        [{"payment_detail_id": f"PD-{order.order_id}-L1",
          "component": "goods", "amount_minor": 100}],
        100, "CNY", f"{order.order_id}-ORPHAN",
    )
    window2_start = _now() - timedelta(hours=2)
    run2 = service.reconcile(auth, merchant_id=merchant, currency="CNY",
                             window_start=window2_start, window_end=window_end,
                             trace=env.trace(order.order_id, "rec-2"))
    assert run2["status"] == "difference"
    assert run2["difference_count"] == 1
    with env.engine.connect() as connection:
        difference = connection.execute(text(
            "SELECT difference_type,amount_minor,environment"
            " FROM reconciliation_difference WHERE tenant_id=:t AND run_id=:r"
        ), {"t": _TENANT, "r": run2["run_id"]}).mappings().first()
        run_env = connection.execute(text(
            "SELECT environment FROM reconciliation_run WHERE tenant_id=:t"
            " AND run_id=:r"
        ), {"t": _TENANT, "r": run["run_id"]}).scalar()
    assert difference["difference_type"] == "channel_only"
    assert int(difference["amount_minor"]) == 100
    assert difference["environment"] == "simulation"
    assert run_env == "simulation"
