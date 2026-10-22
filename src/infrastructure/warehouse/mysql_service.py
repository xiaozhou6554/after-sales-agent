"""步骤25A 仓储退货验收应用服务（开发 Mock 装配）。

- 收货/验收要求仓储员工身份、warehouse:receive/inspect 权限、内容确认、
  幂等键与 expected_revision；主管/模型无写入通道；
- WMS 源记录经独立事务适配器登记，重放按确定性源 ID 返回原记录；
- 售后侧守恒：实收不超批准、质检不超未检实收、部分验收可恢复；
- 结果与事件同事务提交，environment 显式标记 simulation（TC-109）。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from application.common.authorization import (
    GENERIC_DENIED_MESSAGE,
    AuthorizationDenied,
    require_customer_access,
    verify_tool_context,
)
from application.common.context import AuthContext, OperationContext, TraceContext
from application.common.errors import ErrorCode
from application.common.events import (
    EventEnvelope,
    ReturnCreatedPayload,
    WarehouseReturnInspectedPayload,
    WarehouseReturnReceivedPayload,
)
from application.common.operation_confirmation import (
    OperationConfirmationUseCase,
    content_digest,
)
from application.common.warehouse_errors import WarehouseConflict
from domain.warehouse.return_order import (
    ReturnLine,
    ReturnOrder,
    WarehouseStateError,
    requires_return,
)
from infrastructure.messaging.idempotency_store import MySQLIdempotencyStore
from infrastructure.messaging.outbox_repository import OutboxRepository
from infrastructure.security.confirmation_store import MySQLConfirmationStore
from infrastructure.warehouse.mock_wms_adapter import MockWarehouseReturnAdapter

CONFIRMATION_TTL_S = 300
EVENT_PRODUCERS = {
    "return.created": "after-sales-service",
    "warehouse.return.received": "wms-adapter",
    "warehouse.return.inspected": "wms-adapter",
}


@dataclass(frozen=True)
class ReturnProgress:
    return_id: str
    refund_request_id: str
    order_id: str
    customer_id: str
    status: str
    revision: int
    environment: str
    lines: list[dict]

    def as_dict(self) -> dict:
        return {
            "return_id": self.return_id,
            "refund_request_id": self.refund_request_id,
            "order_id": self.order_id,
            "status": self.status,
            "revision": self.revision,
            "environment": self.environment,
            "lines": self.lines,
        }


def _operation_id(binding: OperationContext, idempotency_key: str) -> str:
    return uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"{binding.tenant_id}\x00{binding.actor_subject_id}\x00"
        f"{binding.operation_type}\x00{idempotency_key}",
    ).hex


class MySqlWarehouseService:
    def __init__(
        self,
        engine: Engine,
        adapter: MockWarehouseReturnAdapter,
        outbox: OutboxRepository | None = None,
    ) -> None:
        self._engine = engine
        self._adapter = adapter
        self._outbox = outbox
        self._confirmations = MySQLConfirmationStore(engine)
        self._flow = OperationConfirmationUseCase(self._confirmations, CONFIRMATION_TTL_S)
        self._idempotency = MySQLIdempotencyStore(engine)

    # ---------- 查询 ----------

    def query_progress(self, auth: AuthContext, return_id: str) -> ReturnProgress:
        verify_tool_context(auth, ("return:read",))
        with self._engine.connect() as connection:
            order = connection.execute(text(
                "SELECT refund_request_id,order_id,customer_id,status,revision,environment"
                " FROM return_order WHERE tenant_id=:tenant AND return_id=:return_id"
            ), {"tenant": auth.tenant_id, "return_id": return_id}).mappings().first()
            if order is None:
                raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
            require_customer_access(auth, order["customer_id"], tenant_id=auth.tenant_id)
            lines = connection.execute(text(
                "SELECT line_id,order_line_id,rma_id,approved_qty,received_qty,"
                " accepted_qty,rejected_qty FROM return_line"
                " WHERE tenant_id=:tenant AND return_id=:return_id ORDER BY line_id"
            ), {"tenant": auth.tenant_id, "return_id": return_id}).mappings().all()
        return ReturnProgress(
            return_id=return_id,
            refund_request_id=order["refund_request_id"],
            order_id=order["order_id"],
            customer_id=order["customer_id"],
            status=order["status"],
            revision=int(order["revision"]),
            environment=order["environment"],
            lines=[dict(row) for row in lines],
        )

    # ---------- 建退货单 ----------

    def create_return(
        self,
        auth: AuthContext,
        refund_request_id: str,
        *,
        idempotency_key: str,
        trace: TraceContext,
    ) -> ReturnProgress:
        self._require_warehouse(auth, "warehouse:receive")
        self._trace_ok(trace)
        digest = content_digest({"refund_request_id": refund_request_id})
        with self._engine.begin() as connection:
            idem = self._idempotency.bind(connection).reserve(
                auth.tenant_id, auth.subject_id, "warehouse_return", idempotency_key, digest
            )
            if idem.status == "succeeded":
                existing = self._load(connection, auth.tenant_id, idem.result_ref)
                if existing is None or existing.refund_request_id != refund_request_id:
                    raise WarehouseConflict("原退货登记已变化，请查询进度")
                return existing
            if idem.status != "pending":
                raise WarehouseConflict("原退货登记待核实")
            request = connection.execute(text(
                "SELECT order_id,customer_id,reason_code,review_status"
                " FROM refund_request"
                " WHERE tenant_id=:tenant AND refund_request_id=:rid FOR UPDATE"
            ), {"tenant": auth.tenant_id, "rid": refund_request_id}).mappings().first()
            if request is None or request["review_status"] != "approved":
                raise WarehouseConflict("退货只允许基于已批准的退款申请创建")
            require_customer_access(auth, request["customer_id"], tenant_id=auth.tenant_id)
            request_lines = connection.execute(text(
                "SELECT line_id,quantity FROM refund_request_line"
                " WHERE tenant_id=:tenant AND refund_request_id=:rid ORDER BY line_id"
            ), {"tenant": auth.tenant_id, "rid": refund_request_id}).mappings().all()
            if not request_lines or not requires_return(
                request["reason_code"], int(request_lines[0]["quantity"])
            ):
                raise WarehouseConflict("该申请按政策无需退货验收，不得创建退货单")
            dup = connection.execute(text(
                "SELECT return_id FROM return_order"
                " WHERE tenant_id=:tenant AND refund_request_id=:rid"
            ), {"tenant": auth.tenant_id, "rid": refund_request_id}).first()
            if dup is not None:
                raise WarehouseConflict("该退款申请已存在退货单")
        # 独立 WMS 事务：登记 RMA（确定性 ID，可重放）。
        registrations = self._adapter.create_return(
            auth.tenant_id,
            refund_request_id,
            [
                {
                    "order_id": request["order_id"],
                    "order_line_id": row["line_id"],
                    "approved_qty": int(row["quantity"]),
                }
                for row in request_lines
            ],
            idempotency_key,
        )
        rma_by_line = {item.order_line_id: item.rma_id for item in registrations}
        return_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{auth.tenant_id}\x00return\x00{refund_request_id}",
        ).hex
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with self._engine.begin() as connection:
            request = connection.execute(text(
                "SELECT order_id,customer_id,review_status FROM refund_request"
                " WHERE tenant_id=:tenant AND refund_request_id=:rid FOR UPDATE"
            ), {"tenant": auth.tenant_id, "rid": refund_request_id}).mappings().first()
            if request is None or request["review_status"] != "approved":
                raise WarehouseConflict("退款批准状态已变化，退货登记回滚")
            connection.execute(text(
                "INSERT INTO return_order (tenant_id,return_id,refund_request_id,order_id,"
                " customer_id,created_by,status,revision,environment,created_at,updated_at)"
                " VALUES (:tenant,:return_id,:rid,:order_id,:customer,:actor,'open',0,"
                " :environment,:now,:now)"
            ), {
                "tenant": auth.tenant_id, "return_id": return_id, "rid": refund_request_id,
                "order_id": request["order_id"],
                "customer": request["customer_id"], "actor": auth.subject_id,
                "environment": self._adapter.environment, "now": now,
            })
            for row in request_lines:
                connection.execute(text(
                    "INSERT INTO return_line (tenant_id,return_id,line_id,order_line_id,"
                    " rma_id,approved_qty) VALUES (:tenant,:return_id,:line,:oline,:rma,:qty)"
                ), {
                    "tenant": auth.tenant_id, "return_id": return_id,
                    "line": row["line_id"], "oline": row["line_id"],
                    "rma": rma_by_line[row["line_id"]], "qty": int(row["quantity"]),
                })
            self._idempotency.bind(connection).complete(idem, "succeeded", return_id)
            self._enqueue(connection, auth, trace, "return.created", "return_order", return_id,
                          ReturnCreatedPayload(
                              return_id=return_id, refund_request_id=refund_request_id,
                              order_id=self._order_of(connection, auth.tenant_id,
                                                      refund_request_id),
                              request_line_refs=[row["line_id"] for row in request_lines],
                              revision=0, environment=self._adapter.environment,
                              actor_subject_id=auth.subject_id,
                              customer_id=request["customer_id"],
                          ))
        progress = self.query_progress(auth, return_id)
        return progress

    # ---------- 收货 ----------

    def prepare_receipt(
        self, auth: AuthContext, return_id: str, lines: list[dict], expected_revision: int
    ) -> dict:
        self._require_warehouse(auth, "warehouse:receive")
        binding = self._binding(auth, return_id, "warehouse:receive", lines, expected_revision)
        prepared = self._flow.prepare(binding)
        return {
            "confirmation_id": prepared.record.confirmation_id,
            "expires_at": prepared.record.expires_at.isoformat(),
            "mock_challenge": prepared.mock_challenge,
        }

    def record_receipt(
        self,
        auth: AuthContext,
        return_id: str,
        lines: list[dict],
        *,
        expected_revision: int,
        confirmation_id: str,
        idempotency_key: str,
        trace: TraceContext,
    ) -> dict:
        self._require_warehouse(auth, "warehouse:receive")
        self._trace_ok(trace)
        self._lines_ok(lines, ("line_id", "received_qty"))
        binding = self._binding(auth, return_id, "warehouse:receive", lines, expected_revision)
        operation_id = _operation_id(binding, idempotency_key)
        consumed = self._require_confirmation(binding, confirmation_id, operation_id)
        digest = content_digest(
            {"return_id": return_id, "lines": lines, "expected_revision": expected_revision}
        )
        replay = self._replay_rows(auth.tenant_id, "warehouse_receipt", operation_id)
        if consumed or replay is not None:
            if replay is None:
                raise WarehouseConflict("已消费确认缺少原收货结果")
            return {**replay, "replayed": True}
        with self._engine.begin() as connection:
            idem = self._idempotency.bind(connection).reserve(
                auth.tenant_id, auth.subject_id, "warehouse_receipt", idempotency_key, digest
            )
            if idem.status == "succeeded":
                result = self._replay_rows(auth.tenant_id, "warehouse_receipt", operation_id)
                if result is None:
                    raise WarehouseConflict("原收货结果待查询")
                return {**result, "replayed": True}
            if idem.status != "pending":
                raise WarehouseConflict("原收货结果待核实")
            order = self._load(connection, auth.tenant_id, return_id)
            if order is None:
                raise WarehouseConflict("退货单不存在或不在授权范围内")
            aggregate = self._aggregate(connection, auth.tenant_id, order)
            aggregate = self._receipt_mutations(aggregate, lines)
        # 独立 WMS 事务：登记收货（确定性源 ID）。
        receipts = []
        for line in lines:
            rma_id = self._rma_of(auth.tenant_id, return_id, line["line_id"])
            record = self._adapter.record_receipt(
                auth.tenant_id, rma_id, int(line["received_qty"]), idempotency_key
            )
            receipts.append((line, record))
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with self._engine.begin() as connection:
            order = self._load(connection, auth.tenant_id, return_id)
            if order is None or order.revision != expected_revision:
                raise WarehouseConflict("退货单版本已变化，收货已回滚，请按原幂等键重试")
            aggregate = self._aggregate(connection, auth.tenant_id, order)
            aggregate = self._receipt_mutations(aggregate, lines)
            updated = connection.execute(text(
                "UPDATE return_order SET status=:status,revision=revision+1,"
                " updated_at=:now WHERE tenant_id=:tenant AND return_id=:return_id"
                " AND revision=:expected"
            ), {
                "status": aggregate.status, "now": now, "tenant": auth.tenant_id,
                "return_id": return_id, "expected": expected_revision,
            })
            if updated.rowcount != 1:
                raise WarehouseConflict("退货单版本冲突")
            for line, record in receipts:
                connection.execute(text(
                    "INSERT INTO warehouse_receipt (tenant_id,operation_id,return_id,line_id,"
                    " received_qty,source_record_id,source_version,actor_subject_id,"
                    " idempotency_key,environment,created_at)"
                    " VALUES (:tenant,:operation,:return_id,:line,:qty,:source,'1',:actor,"
                    " :idem,:environment,:now)"
                ), {
                    "tenant": auth.tenant_id, "operation": operation_id,
                    "return_id": return_id, "line": line["line_id"],
                    "qty": int(line["received_qty"]), "source": record.receipt_id,
                    "actor": auth.subject_id, "idem": idempotency_key,
                    "environment": self._adapter.environment, "now": now,
                })
                connection.execute(text(
                    "UPDATE return_line SET received_qty=received_qty+:qty"
                    " WHERE tenant_id=:tenant AND return_id=:return_id AND line_id=:line"
                ), {
                    "tenant": auth.tenant_id, "return_id": return_id,
                    "line": line["line_id"], "qty": int(line["received_qty"]),
                })
            self._confirmations.bind(connection).consume(
                confirmation_id, binding, operation_id, datetime.now(timezone.utc)
            )
            self._idempotency.bind(connection).complete(idem, "succeeded", return_id)
            self._enqueue(
                connection, auth, trace, "warehouse.return.received", "return_order", return_id,
                WarehouseReturnReceivedPayload(
                    return_id=return_id, receipt_id=receipts[0][1].receipt_id,
                    receipt_line_refs=[line["line_id"] for line, _ in receipts],
                    received_qty=sum(int(line["received_qty"]) for line, _ in receipts),
                    source_version="1", evidence_refs=[r.receipt_id for _, r in receipts],
                    environment=self._adapter.environment,
                    actor_subject_id=auth.subject_id, customer_id=order.customer_id,
                ),
            )
        self._audit(auth, trace, "warehouse.receipt", return_id, lines)
        return {
            "operation_id": operation_id, "return_id": return_id,
            "status": aggregate.status, "revision": expected_revision + 1,
            "receipt_ids": [record.receipt_id for _, record in receipts],
            "environment": self._adapter.environment, "replayed": False,
        }

    # ---------- 质检验收 ----------

    def prepare_inspection(
        self, auth: AuthContext, return_id: str, lines: list[dict], expected_revision: int
    ) -> dict:
        self._require_warehouse(auth, "warehouse:inspect")
        binding = self._binding(auth, return_id, "warehouse:inspect", lines, expected_revision)
        prepared = self._flow.prepare(binding)
        return {
            "confirmation_id": prepared.record.confirmation_id,
            "expires_at": prepared.record.expires_at.isoformat(),
            "mock_challenge": prepared.mock_challenge,
        }

    def record_inspection(
        self,
        auth: AuthContext,
        return_id: str,
        lines: list[dict],
        *,
        expected_revision: int,
        confirmation_id: str,
        idempotency_key: str,
        trace: TraceContext,
    ) -> dict:
        self._require_warehouse(auth, "warehouse:inspect")
        self._trace_ok(trace)
        self._lines_ok(lines, ("line_id", "accepted_qty", "rejected_qty"))
        for line in lines:
            if not all(
                isinstance(line.get(field), str) and line.get(field)
                for field in ("line_id",)
            ):
                raise WarehouseConflict("质检行参数无效")
        binding = self._binding(auth, return_id, "warehouse:inspect", lines, expected_revision)
        operation_id = _operation_id(binding, idempotency_key)
        consumed = self._require_confirmation(binding, confirmation_id, operation_id)
        digest = content_digest(
            {"return_id": return_id, "lines": lines, "expected_revision": expected_revision}
        )
        replay = self._replay_rows(auth.tenant_id, "warehouse_inspection", operation_id)
        if consumed or replay is not None:
            if replay is None:
                raise WarehouseConflict("已消费确认缺少原质检结果")
            return {**replay, "replayed": True}
        with self._engine.begin() as connection:
            idem = self._idempotency.bind(connection).reserve(
                auth.tenant_id, auth.subject_id, "warehouse_inspection", idempotency_key, digest
            )
            if idem.status == "succeeded":
                result = self._replay_rows(auth.tenant_id, "warehouse_inspection", operation_id)
                if result is None:
                    raise WarehouseConflict("原质检结果待查询")
                return {**result, "replayed": True}
            if idem.status != "pending":
                raise WarehouseConflict("原质检结果待核实")
            order = self._load(connection, auth.tenant_id, return_id)
            if order is None:
                raise WarehouseConflict("退货单不存在或不在授权范围内")
            aggregate = self._aggregate(connection, auth.tenant_id, order)
            aggregate = self._inspection_mutations(aggregate, lines)
        # 独立 WMS 事务：登记质检。
        inspections = []
        for line in lines:
            rma_id = self._rma_of(auth.tenant_id, return_id, line["line_id"])
            receipt_id = self._last_receipt(auth.tenant_id, rma_id)
            record = self._adapter.record_inspection(
                auth.tenant_id, rma_id, receipt_id,
                int(line["accepted_qty"]), int(line["rejected_qty"]),
                [str(line.get("reason_code", "inspection_result"))], idempotency_key,
            )
            inspections.append((line, record, receipt_id))
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with self._engine.begin() as connection:
            order = self._load(connection, auth.tenant_id, return_id)
            if order is None or order.revision != expected_revision:
                raise WarehouseConflict("退货单版本已变化，质检已回滚，请按原幂等键重试")
            aggregate = self._aggregate(connection, auth.tenant_id, order)
            aggregate = self._inspection_mutations(aggregate, lines)
            updated = connection.execute(text(
                "UPDATE return_order SET status=:status,revision=revision+1,"
                " updated_at=:now WHERE tenant_id=:tenant AND return_id=:return_id"
                " AND revision=:expected"
            ), {
                "status": aggregate.status, "now": now, "tenant": auth.tenant_id,
                "return_id": return_id, "expected": expected_revision,
            })
            if updated.rowcount != 1:
                raise WarehouseConflict("退货单版本冲突")
            for line, record, receipt_id in inspections:
                connection.execute(text(
                    "INSERT INTO warehouse_inspection (tenant_id,operation_id,return_id,"
                    " line_id,accepted_qty,rejected_qty,reason_codes,source_record_id,"
                    " source_version,actor_subject_id,idempotency_key,environment,created_at)"
                    " VALUES (:tenant,:operation,:return_id,:line,:accepted,:rejected,"
                    " :reasons,:source,'1',:actor,:idem,:environment,:now)"
                ), {
                    "tenant": auth.tenant_id, "operation": operation_id,
                    "return_id": return_id, "line": line["line_id"],
                    "accepted": int(line["accepted_qty"]),
                    "rejected": int(line["rejected_qty"]),
                    "reasons": json.dumps(
                        [str(line.get("reason_code", "inspection_result"))]
                    ),
                    "source": record.inspection_id, "actor": auth.subject_id,
                    "idem": idempotency_key, "environment": self._adapter.environment,
                    "now": now,
                })
                connection.execute(text(
                    "UPDATE return_line SET accepted_qty=accepted_qty+:accepted,"
                    " rejected_qty=rejected_qty+:rejected"
                    " WHERE tenant_id=:tenant AND return_id=:return_id AND line_id=:line"
                ), {
                    "tenant": auth.tenant_id, "return_id": return_id,
                    "line": line["line_id"], "accepted": int(line["accepted_qty"]),
                    "rejected": int(line["rejected_qty"]),
                })
            self._confirmations.bind(connection).consume(
                confirmation_id, binding, operation_id, datetime.now(timezone.utc)
            )
            self._idempotency.bind(connection).complete(idem, "succeeded", return_id)
            self._enqueue(
                connection, auth, trace, "warehouse.return.inspected", "return_order", return_id,
                WarehouseReturnInspectedPayload(
                    return_id=return_id, inspection_id=inspections[0][1].inspection_id,
                    receipt_line_refs=[line["line_id"] for line, _, _ in inspections],
                    accepted_qty=sum(int(line["accepted_qty"]) for line, _, _ in inspections),
                    rejected_qty=sum(int(line["rejected_qty"]) for line, _, _ in inspections),
                    reason_codes=[str(line.get("reason_code", "inspection_result"))
                                  for line, _, _ in inspections],
                    source_version="1",
                    evidence_refs=[record.inspection_id for _, record, _ in inspections],
                    environment=self._adapter.environment,
                    actor_subject_id=auth.subject_id, customer_id=order.customer_id,
                ),
            )
        self._audit(auth, trace, "warehouse.inspection", return_id, lines)
        return {
            "operation_id": operation_id, "return_id": return_id,
            "status": aggregate.status, "revision": expected_revision + 1,
            "inspection_ids": [record.inspection_id for _, record, _ in inspections],
            "environment": self._adapter.environment, "replayed": False,
        }

    # ---------- 内部工具 ----------

    @staticmethod
    def _receipt_mutations(aggregate: ReturnOrder, lines: list[dict]) -> ReturnOrder:
        try:
            for line in lines:
                aggregate = aggregate.record_receipt(
                    line["line_id"], int(line["received_qty"])
                )
        except WarehouseStateError as exc:
            raise WarehouseConflict(str(exc)) from exc
        return aggregate

    @staticmethod
    def _inspection_mutations(aggregate: ReturnOrder, lines: list[dict]) -> ReturnOrder:
        try:
            for line in lines:
                aggregate = aggregate.record_inspection(
                    line["line_id"], int(line["accepted_qty"]), int(line["rejected_qty"])
                )
        except WarehouseStateError as exc:
            raise WarehouseConflict(str(exc)) from exc
        return aggregate

    def _require_warehouse(self, auth: AuthContext, scope: str) -> None:
        verify_tool_context(auth, (scope,))
        if "warehouse" not in auth.roles:
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)

    @staticmethod
    def _trace_ok(trace: TraceContext) -> None:
        if not isinstance(trace, TraceContext) or not all(
            (trace.request_id, trace.correlation_id, trace.trace_id)
        ):
            raise WarehouseConflict("缺少可信链路标识")

    @staticmethod
    def _lines_ok(lines: list[dict], fields: tuple[str, ...]) -> None:
        if not isinstance(lines, list) or not lines:
            raise WarehouseConflict("操作行不能为空")
        for line in lines:
            if not isinstance(line, dict):
                raise WarehouseConflict("操作行参数无效")
            for field in fields:
                value = line.get(field)
                if field == "line_id":
                    if not isinstance(value, str) or not value:
                        raise WarehouseConflict("操作行标识无效")
                elif not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise WarehouseConflict(f"{field} 必须为非负整数")

    def _binding(
        self,
        auth: AuthContext,
        return_id: str,
        operation_type: str,
        lines: list[dict],
        expected_revision: int,
    ) -> OperationContext:
        with self._engine.connect() as connection:
            row = connection.execute(text(
                "SELECT customer_id FROM return_order"
                " WHERE tenant_id=:tenant AND return_id=:return_id"
            ), {"tenant": auth.tenant_id, "return_id": return_id}).first()
        if row is None:
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
        require_customer_access(auth, row[0], tenant_id=auth.tenant_id)
        return OperationContext.from_trusted_actor(
            auth,
            customer_id=row[0],
            confirmer_subject_id=auth.subject_id,
            operation_type=operation_type,
            target_id=return_id,
            payload_digest=content_digest(
                {"return_id": return_id, "lines": lines,
                 "expected_revision": expected_revision}
            ),
            expected_revision=expected_revision,
        )

    def _require_confirmation(
        self, binding: OperationContext, confirmation_id: str, operation_id: str
    ) -> bool:
        if not isinstance(confirmation_id, str) or not confirmation_id:
            raise WarehouseConflict("缺少内容确认凭证")
        record = self._confirmations.get(binding.tenant_id, confirmation_id)
        if record is None or record.binding != binding:
            raise WarehouseConflict("确认凭证与本次收货内容不匹配")
        if record.status == "consumed" and record.consumed_by_operation_id == operation_id:
            return True
        if record.status != "verified" or record.expires_at <= datetime.now(timezone.utc):
            raise WarehouseConflict("确认未验证或已过期")
        return False

    def _load(self, connection: Connection, tenant_id: str, return_id: str):
        return connection.execute(text(
            "SELECT return_id,refund_request_id,order_id,customer_id,status,revision,"
            " environment FROM return_order"
            " WHERE tenant_id=:tenant AND return_id=:return_id FOR UPDATE"
        ), {"tenant": tenant_id, "return_id": return_id}).mappings().first()

    def _aggregate(
        self, connection: Connection, tenant_id: str, order
    ) -> ReturnOrder:
        rows = connection.execute(text(
            "SELECT line_id,order_line_id,rma_id,approved_qty,received_qty,"
            " accepted_qty,rejected_qty FROM return_line"
            " WHERE tenant_id=:tenant AND return_id=:return_id ORDER BY line_id"
        ), {"tenant": tenant_id, "return_id": order["return_id"]}).mappings().all()
        return ReturnOrder(
            return_id=order["return_id"],
            refund_request_id=order["refund_request_id"],
            order_id=order["order_id"],
            customer_id=order["customer_id"],
            status=order["status"],
            revision=int(order["revision"]),
            environment=order["environment"],
            lines=tuple(
                ReturnLine(
                    row["line_id"], row["order_line_id"], row["rma_id"],
                    int(row["approved_qty"]), int(row["received_qty"]),
                    int(row["accepted_qty"]), int(row["rejected_qty"]),
                )
                for row in rows
            ),
        )

    def _rma_of(self, tenant_id: str, return_id: str, line_id: str) -> str:
        with self._engine.connect() as connection:
            row = connection.execute(text(
                "SELECT rma_id FROM return_line"
                " WHERE tenant_id=:tenant AND return_id=:return_id AND line_id=:line"
            ), {"tenant": tenant_id, "return_id": return_id, "line": line_id}).first()
        if row is None:
            raise WarehouseConflict("退货单行不存在")
        return row[0]

    def _last_receipt(self, tenant_id: str, rma_id: str) -> str:
        with self._engine.connect() as connection:
            row = connection.execute(text(
                "SELECT receipt_id FROM mock_wms_receipt"
                " WHERE tenant_id=:tenant AND rma_id=:rma"
                " ORDER BY received_quantity DESC, receipt_id DESC LIMIT 1"
            ), {"tenant": tenant_id, "rma": rma_id}).first()
        if row is None:
            raise WarehouseConflict("该退货行尚无 WMS 收货记录，不能质检")
        return row[0]

    def _replay_rows(self, tenant_id: str, table: str, operation_id: str) -> dict | None:
        with self._engine.connect() as connection:
            rows = connection.execute(text(
                f"SELECT line_id,created_at FROM {table}"
                f" WHERE tenant_id=:tenant AND operation_id=:operation"
            ), {"tenant": tenant_id, "operation": operation_id}).mappings().all()
        if not rows:
            return None
        return {
            "operation_id": operation_id,
            "line_ids": [row["line_id"] for row in rows],
            "created_at": rows[0]["created_at"].isoformat(),
        }

    def _order_of(self, connection: Connection, tenant_id: str, refund_request_id: str) -> str:
        row = connection.execute(text(
            "SELECT order_id FROM refund_request"
            " WHERE tenant_id=:tenant AND refund_request_id=:rid"
        ), {"tenant": tenant_id, "rid": refund_request_id}).first()
        if row is None:
            raise WarehouseConflict("退款申请不存在")
        return row[0]

    def _enqueue(
        self,
        connection: Connection,
        auth: AuthContext,
        trace: TraceContext,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload,
    ) -> None:
        if self._outbox is None:
            raise WarehouseConflict("事件出口未装配，操作失败关闭")
        event = EventEnvelope(
            schema_version="1.0",
            event_id=uuid.uuid4().hex,
            event_type=event_type,
            event_version=1,
            tenant_id=auth.tenant_id,
            request_id=trace.request_id,
            correlation_id=trace.correlation_id,
            trace_id=trace.trace_id,
            operation_id=uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{auth.tenant_id}:{aggregate_type}:{aggregate_id}:{event_type}:"
                f"{trace.request_id}",
            ).hex,
            causation_id=None,
            parent_event_id=None,
            producer=EVENT_PRODUCERS[event_type],
            occurred_at=datetime.now(timezone.utc).isoformat(),
            idempotency_key=f"{event_type}:{aggregate_id}:{trace.request_id}",
            subject_id=auth.subject_id,
            retry_count=0,
            payload=payload,
        )
        self._outbox.enqueue(
            connection, event, aggregate_type=aggregate_type, aggregate_id=aggregate_id,
            thread_id=trace.thread_id,
        )

    def _audit(
        self, auth: AuthContext, trace: TraceContext, action: str,
        return_id: str, lines: list[dict],
    ) -> None:
        with self._engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO agent_audit_log (audit_id,tenant_id,request_id,correlation_id,"
                " trace_id,thread_id,actor_subject_id,customer_id,action,resource_refs,"
                " parameter_summary,result_summary,status)"
                " VALUES (:audit_id,:tenant,:request,:correlation,:trace,:thread,:actor,"
                " NULL,:action,:refs,:params,:result,'success')"
            ), {
                "audit_id": uuid.uuid4().hex, "tenant": auth.tenant_id,
                "request": trace.request_id, "correlation": trace.correlation_id,
                "trace": trace.trace_id, "thread": trace.thread_id,
                "actor": auth.subject_id, "action": action,
                "refs": json.dumps([f"return:{return_id}"]),
                "params": json.dumps({"lines": lines}, ensure_ascii=False),
                "result": json.dumps({"return_id": return_id}),
            })
