"""步骤25A 集成验收：分批收货/部分验收/守恒/幂等重放/权限/TC-109 模拟标记。

种子数据直接写入已批准的 refund_request（绕过 25 步 MCP 链路），收货与质检
走 MySqlWarehouseService 真实确认、幂等与独立 WMS 事务。
"""

import uuid

import pytest
from bootstrap.settings import load_settings
from sqlalchemy import text

from application.common.audit import AuditUseCase
from application.common.authorization import AuthorizationDenied
from application.common.context import TraceContext
from application.common.operation_confirmation import OperationConfirmationUseCase
from application.common.warehouse_errors import WarehouseConflict
from application.order.use_cases.order_query import QueryOrderUseCase, QueryPaymentUseCase
from infrastructure.auth.mock_auth_adapter import MockAuthAdapter
from infrastructure.messaging.event_contract import EventCatalog
from infrastructure.messaging.outbox_repository import OutboxRepository
from infrastructure.mock.order_adapter import MockOrderAdapter
from infrastructure.observability.audit_repository import MySQLAuditRepository
from infrastructure.persistence.mysql.connection import create_mysql_engine
from infrastructure.security.confirmation_store import MySQLConfirmationStore
from infrastructure.warehouse.mock_wms_adapter import MockWarehouseReturnAdapter
from infrastructure.warehouse.mysql_service import MySqlWarehouseService
from interface.mcp.context_guard import ToolContextGuard
from interface.mcp.tools import MCPToolDeps, execute_tool_call

_TENANT = "tenant-north"


def _trace(tag: str) -> TraceContext:
    run = uuid.uuid4().hex[:8]
    return TraceContext(
        "1.0", f"req-{tag}-{run}", f"corr-{tag}-{run}", f"trace-{tag}-{run}", None
    )


@pytest.fixture
def warehouse_env():
    settings = load_settings()
    engine = create_mysql_engine(
        settings.mysql_url, settings.mysql_connect_timeout_s, settings.mysql_read_timeout_s
    )
    order_id = f"AT-WH-{uuid.uuid4().hex[:10]}"
    auth_gateway = MockAuthAdapter.from_fixture("data/fixtures/mock-identities.json", enabled=True)
    warehouse = auth_gateway.issue_mock_session("wh-north-01")
    customer = auth_gateway.issue_mock_session("cust-north-01")
    service = MySqlWarehouseService(
        engine,
        MockWarehouseReturnAdapter(engine),
        OutboxRepository(
            engine, EventCatalog("data/contracts/event-catalog.json",
                                 "data/contracts/events.schema.json")
        ),
    )
    confirmations = OperationConfirmationUseCase(MySQLConfirmationStore(engine), 300)
    try:
        yield engine, service, confirmations, warehouse, customer, order_id
    finally:
        marker = f"%{order_id}%"
        rid_sub = (
            "SELECT refund_request_id FROM refund_request"
            f" WHERE tenant_id='{_TENANT}' AND order_id=:o"
        )
        ret_sub = (
            "SELECT return_id FROM return_order"
            f" WHERE tenant_id='{_TENANT}' AND refund_request_id IN ({rid_sub})"
        )
        rma_sub = (
            "SELECT rma_id FROM mock_wms_return_authorization"
            f" WHERE tenant_id='{_TENANT}' AND order_id=:o"
        )
        with engine.begin() as connection:
            for statement, params in (
                ("DELETE FROM consumer_inbox WHERE tenant_id=:t AND event_id IN"
                 " (SELECT event_id FROM outbox_event WHERE tenant_id=:t AND payload LIKE :m)",
                 {"t": _TENANT, "m": marker}),
                ("DELETE FROM outbox_event WHERE tenant_id=:t AND payload LIKE :m",
                 {"t": _TENANT, "m": marker}),
                ("DELETE FROM agent_audit_log WHERE tenant_id=:t AND parameter_summary LIKE :m",
                 {"t": _TENANT, "m": marker}),
                ("DELETE FROM operation_confirmation WHERE tenant_id=:t"
                 " AND operation_type LIKE 'warehouse%'"
                 f" AND target_id IN ({ret_sub})",
                 {"t": _TENANT, "o": order_id}),
                ("DELETE FROM operation_idempotency WHERE tenant_id=:t"
                 " AND actor_subject_id='warehouse-north-01' AND idempotency_key LIKE :m",
                 {"t": _TENANT, "m": marker}),
                (f"DELETE FROM warehouse_inspection WHERE tenant_id=:t"
                 f" AND return_id IN ({ret_sub})",
                 {"t": _TENANT, "o": order_id}),
                (f"DELETE FROM warehouse_receipt WHERE tenant_id=:t AND return_id IN ({ret_sub})",
                 {"t": _TENANT, "o": order_id}),
                (f"DELETE FROM return_line WHERE tenant_id=:t AND return_id IN ({ret_sub})",
                 {"t": _TENANT, "o": order_id}),
                (f"DELETE FROM return_order WHERE tenant_id=:t"
                 f" AND refund_request_id IN ({rid_sub})",
                 {"t": _TENANT, "o": order_id}),
                (f"DELETE FROM mock_wms_inspection WHERE tenant_id=:t AND receipt_id IN"
                 f" (SELECT receipt_id FROM mock_wms_receipt WHERE tenant_id=:t"
                 f" AND rma_id IN ({rma_sub}))",
                 {"t": _TENANT, "o": order_id}),
                (f"DELETE FROM mock_wms_receipt WHERE tenant_id=:t AND rma_id IN ({rma_sub})",
                 {"t": _TENANT, "o": order_id}),
                ("DELETE FROM mock_wms_return_authorization WHERE tenant_id=:t AND order_id=:o",
                 {"t": _TENANT, "o": order_id}),
                ("DELETE FROM mock_oms_order_line WHERE tenant_id=:t AND order_id=:o",
                 {"t": _TENANT, "o": order_id}),
                ("DELETE FROM mock_oms_order WHERE tenant_id=:t AND order_id=:o",
                 {"t": _TENANT, "o": order_id}),
                (f"DELETE FROM refund_request_line WHERE tenant_id=:t"
                 f" AND refund_request_id IN ({rid_sub})",
                 {"t": _TENANT, "o": order_id}),
                ("DELETE FROM refund_request WHERE tenant_id=:t AND order_id=:o",
                 {"t": _TENANT, "o": order_id}),
            ):
                connection.execute(text(statement), params)
        engine.dispose()


def _seed_approved_return(engine, order_id: str, reason_code: str) -> str:
    refund_request_id = f"RR-{order_id}"
    now = "NOW(6)"
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO mock_oms_order"
            " (tenant_id,order_id,customer_id,status,currency,paid_minor,version)"
            " VALUES (:t,:order,'CUSTOMER-01','pending_shipment','CNY',3000,1)"
        ), {"t": _TENANT, "order": order_id})
        for line_id, qty, goods in (("L1", 2, 2000), ("L2", 1, 1000)):
            connection.execute(text(
                "INSERT INTO mock_oms_order_line"
                " (tenant_id,order_id,line_id,source_product_id,quantity,"
                " goods_minor,shipping_minor)"
                " VALUES (:t,:order,:line,'P-WH-01',:qty,:goods,0)"
            ), {"t": _TENANT, "order": order_id, "line": line_id, "qty": qty, "goods": goods})
        connection.execute(text(
            "INSERT INTO refund_request (tenant_id,refund_request_id,quote_id,order_id,"
            " actor_subject_id,customer_id,confirmation_id,idempotency_key,reason_code,"
            " eligibility,review_status,amount_minor,goods_minor,shipping_minor,currency,"
            " policy_id,policy_version,order_version,payment_version,created_at,updated_at)"
            " VALUES (:t,:rid,:quote,:order,'customer-north-01','CUSTOMER-01',"
            " :conf,:idem,:reason,'eligible','approved',3000,2000,1000,'CNY',"
            " 'POL-7D','v2','1','1'," + now + "," + now + ")"
        ), {
            "t": _TENANT, "rid": refund_request_id, "quote": f"Q-{order_id}",
            "order": order_id, "conf": f"C-{order_id}", "idem": f"I-{order_id}",
            "reason": reason_code,
        })
        for line_id, qty in (("L1", 2), ("L2", 1)):
            connection.execute(text(
                "INSERT INTO refund_request_line (tenant_id,refund_request_id,line_id,"
                " quantity,goods_minor,shipping_minor) VALUES (:t,:rid,:line,:qty,:goods,0)"
            ), {
                "t": _TENANT, "rid": refund_request_id, "line": line_id, "qty": qty,
                "goods": 1000 * qty,
            })
    return refund_request_id


def _verified(engine, service, auth, return_id: str, lines: list[dict],
              expected_revision: int, kind: str) -> str:
    prepare = (
        service.prepare_receipt if kind == "warehouse:receive" else service.prepare_inspection
    )
    prepared = prepare(auth, return_id, lines, expected_revision)
    flow = OperationConfirmationUseCase(MySQLConfirmationStore(engine), 300)
    flow.verify(auth.tenant_id, prepared["confirmation_id"], prepared["mock_challenge"])
    return prepared["confirmation_id"]


def test_tc97_receipt_inspection_chain_with_conservation_and_replay(warehouse_env) -> None:
    engine, service, flow, warehouse, customer, order_id = warehouse_env
    refund_request_id = _seed_approved_return(engine, order_id, "no_longer_needed")
    wh_auth, cust_auth = warehouse.auth, customer.auth
    wh_trace = _trace("wh")
    idem = f"{order_id}-RET"

    created = service.create_return(wh_auth, refund_request_id,
                                    idempotency_key=idem, trace=wh_trace)
    assert created.status == "open" and created.revision == 0
    assert created.environment == "simulation"
    return_id = created.return_id
    assert {line["line_id"] for line in created.lines} == {"L1", "L2"}
    assert all(line["rma_id"] for line in created.lines)

    with pytest.raises(WarehouseConflict, match="已存在退货单"):
        service.create_return(wh_auth, refund_request_id,
                              idempotency_key=f"{order_id}-RET-2", trace=wh_trace)

    # TC-98：需退货未验收，客户可查验收进度（pending 状态可见）。
    progress = service.query_progress(cust_auth, return_id)
    assert progress.status == "open" and progress.order_id == order_id

    # MCP 只读通道：模型经 query_return_progress 可见；写入工具不在模型侧。
    deps = MCPToolDeps(
        auth_gateway=MockAuthAdapter.from_fixture(
            "data/fixtures/mock-identities.json", enabled=True
        ),
        guard=ToolContextGuard("data/contracts/mcp-tools.json"),
        query_order=QueryOrderUseCase(MockOrderAdapter(engine)),
        query_payment=QueryPaymentUseCase(MockOrderAdapter(engine)),
        audit=AuditUseCase(MySQLAuditRepository(engine)),
        warehouse_query=service,
    )
    headers = {"authorization": f"Bearer {customer.session_token}"}
    tool_result = execute_tool_call(
        "query_return_progress", {"return_id": return_id}, deps=deps, headers=headers
    )
    assert tool_result["status"] == "success"
    assert tool_result["data"]["return_id"] == return_id
    assert tool_result["data"]["environment"] == "simulation"

    # 分批收货：L1 首件 1 件 → partially_received。
    conf1 = _verified(engine, service, wh_auth, return_id,
                      [{"line_id": "L1", "received_qty": 1}], 0, "warehouse:receive")
    receipt1 = service.record_receipt(
        wh_auth, return_id, [{"line_id": "L1", "received_qty": 1}],
        expected_revision=0, confirmation_id=conf1,
        idempotency_key=f"{order_id}-RCPT-1", trace=wh_trace,
    )
    assert receipt1["status"] == "partially_received" and receipt1["revision"] == 1
    assert receipt1["replayed"] is False
    assert receipt1["environment"] == "simulation"

    # 超收拒绝：L1 再收 2 件超出批准 2 件。
    conf_over = _verified(engine, service, wh_auth, return_id,
                          [{"line_id": "L1", "received_qty": 2}], 1, "warehouse:receive")
    with pytest.raises(WarehouseConflict, match="超收"):
        service.record_receipt(
            wh_auth, return_id, [{"line_id": "L1", "received_qty": 2}],
            expected_revision=1, confirmation_id=conf_over,
            idempotency_key=f"{order_id}-RCPT-OVER", trace=wh_trace,
        )
    assert service.query_progress(wh_auth, return_id).revision == 1

    # 补齐 L1 并收 L2 → received。
    lines2 = [{"line_id": "L1", "received_qty": 1}, {"line_id": "L2", "received_qty": 1}]
    conf2 = _verified(engine, service, wh_auth, return_id, lines2, 1, "warehouse:receive")
    receipt2 = service.record_receipt(
        wh_auth, return_id, lines2, expected_revision=1, confirmation_id=conf2,
        idempotency_key=f"{order_id}-RCPT-2", trace=wh_trace,
    )
    assert receipt2["status"] == "received" and receipt2["revision"] == 2

    # 重复登记幂等：同键重放返回原结果且不新增收货。
    replay = service.record_receipt(
        wh_auth, return_id, lines2, expected_revision=1, confirmation_id=conf2,
        idempotency_key=f"{order_id}-RCPT-2", trace=wh_trace,
    )
    assert replay["replayed"] is True and replay["operation_id"] == receipt2["operation_id"]
    assert service.query_progress(wh_auth, return_id).revision == 2

    # 部分验收：L1 先验 1 件 → partially_inspected。
    lines_i1 = [{"line_id": "L1", "accepted_qty": 1, "rejected_qty": 0, "reason_code": "ok"}]
    conf_i1 = _verified(engine, service, wh_auth, return_id, lines_i1, 2, "warehouse:inspect")
    inspect1 = service.record_inspection(
        wh_auth, return_id, lines_i1,
        expected_revision=2, confirmation_id=conf_i1,
        idempotency_key=f"{order_id}-INSP-1", trace=wh_trace,
    )
    assert inspect1["status"] == "partially_inspected" and inspect1["revision"] == 3

    # 超检拒绝：L2 未检实收 1 件，质检 2 件违反守恒。
    lines_over_i = [{"line_id": "L2", "accepted_qty": 2, "rejected_qty": 0}]
    conf_over_i = _verified(engine, service, wh_auth, return_id,
                            lines_over_i, 3, "warehouse:inspect")
    with pytest.raises(WarehouseConflict, match="未检实收"):
        service.record_inspection(
            wh_auth, return_id,
            [{"line_id": "L2", "accepted_qty": 2, "rejected_qty": 0}],
            expected_revision=3, confirmation_id=conf_over_i,
            idempotency_key=f"{order_id}-INSP-OVER", trace=wh_trace,
        )

    # 完成全部质检 → completed；守恒：实收≤批准、质检合计=实收。
    lines_i2 = [
        {"line_id": "L1", "accepted_qty": 1, "rejected_qty": 0, "reason_code": "ok"},
        {"line_id": "L2", "accepted_qty": 1, "rejected_qty": 0, "reason_code": "ok"},
    ]
    conf_i2 = _verified(engine, service, wh_auth, return_id, lines_i2, 3, "warehouse:inspect")
    inspect2 = service.record_inspection(
        wh_auth, return_id, lines_i2, expected_revision=3, confirmation_id=conf_i2,
        idempotency_key=f"{order_id}-INSP-2", trace=wh_trace,
    )
    assert inspect2["status"] == "completed" and inspect2["revision"] == 4

    final = service.query_progress(wh_auth, return_id)
    by_line = {line["line_id"]: line for line in final.lines}
    assert by_line["L1"]["received_qty"] == 2 and by_line["L1"]["approved_qty"] == 2
    assert by_line["L1"]["accepted_qty"] == 2 and by_line["L1"]["rejected_qty"] == 0
    assert by_line["L2"]["received_qty"] == 1 and by_line["L2"]["accepted_qty"] == 1
    for line in final.lines:
        assert line["received_qty"] <= line["approved_qty"]
        assert line["accepted_qty"] + line["rejected_qty"] <= line["received_qty"]

    # TC-109：售后行、WMS 源行与事件均显式标记 simulation。
    with engine.connect() as connection:
        env = connection.execute(text(
            "SELECT environment FROM return_order"
            " WHERE tenant_id=:t AND return_id=:r"
        ), {"t": _TENANT, "r": return_id}).scalar()
        receipt_env = connection.execute(text(
            "SELECT DISTINCT environment FROM warehouse_receipt"
            " WHERE tenant_id=:t AND return_id=:r"
        ), {"t": _TENANT, "r": return_id}).scalars().all()
        events = connection.execute(text(
            "SELECT event_type, payload FROM outbox_event"
            " WHERE tenant_id=:t AND payload LIKE :m"
        ), {"t": _TENANT, "m": f"%{return_id}%"}).all()
    assert env == "simulation"
    assert receipt_env == ["simulation"]
    event_types = {row[0] for row in events}
    assert {"return.created", "warehouse.return.received",
            "warehouse.return.inspected"} <= event_types
    for _, payload in events:
        assert "simulation" in payload


def test_tc98_quality_dispute_refund_never_creates_return(warehouse_env) -> None:
    engine, service, _flow, warehouse, customer, order_id = warehouse_env
    refund_request_id = _seed_approved_return(engine, order_id, "quality_issue")
    with pytest.raises(WarehouseConflict, match="无需退货"):
        service.create_return(warehouse.auth, refund_request_id,
                              idempotency_key=f"{order_id}-RET", trace=_trace("q"))
    with engine.connect() as connection:
        count = connection.execute(text(
            "SELECT COUNT(*) FROM return_order WHERE tenant_id=:t"
            " AND refund_request_id=:r"
        ), {"t": _TENANT, "r": refund_request_id}).scalar()
    assert count == 0


def test_customer_and_write_scope_gates_block_non_warehouse_actors(warehouse_env) -> None:
    engine, service, _flow, warehouse, customer, order_id = warehouse_env
    refund_request_id = _seed_approved_return(engine, order_id, "no_longer_needed")
    with pytest.raises(AuthorizationDenied):
        service.create_return(customer.auth, refund_request_id,
                              idempotency_key=f"{order_id}-RET", trace=_trace("deny"))
    created = service.create_return(warehouse.auth, refund_request_id,
                                    idempotency_key=f"{order_id}-RET", trace=_trace("ok"))
    with pytest.raises(AuthorizationDenied):
        service.prepare_receipt(customer.auth, created.return_id,
                                [{"line_id": "L1", "received_qty": 1}], 0)
    with pytest.raises(AuthorizationDenied):
        service.prepare_inspection(customer.auth, created.return_id,
                                   [{"line_id": "L1", "accepted_qty": 1,
                                     "rejected_qty": 0}], 0)
