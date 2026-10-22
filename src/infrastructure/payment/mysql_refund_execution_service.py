"""步骤25B 受控退款执行协调服务（开发 Mock 装配）。

- 仅 refund:execute 服务身份可执行；执行前重校客户确认（consumed）、
  批准版本（政策/订单/支付三方版本）、原支付、币种、逐行分摊守恒、
  剩余可退额、必要退货验收与幂等键；
- 渠道调用走独立事务适配器；同一执行 ID 承载提交与恢复，超时/未知
  只查询原执行；只有渠道权威成功证据写入账本并结转预留；
- 回调必须验签且金额/商户匹配，成功不回退；差异记录不自动冲销；
- 结果与事件同事务提交，environment 显式标记 simulation（TC-109）。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from application.common.authorization import (
    GENERIC_DENIED_MESSAGE,
    PAYMENT_ROLE,
    AuthorizationDenied,
    require_customer_access,
    verify_tool_context,
)
from application.common.context import AuthContext, TraceContext
from application.common.errors import ErrorCode
from application.common.events import (
    EventEnvelope,
    RefundExecutionFailedPayload,
    RefundExecutionRequestedPayload,
    RefundExecutionSucceededPayload,
)
from application.common.operation_confirmation import content_digest
from application.common.payment_errors import PaymentConflict
from domain.payment.refund_execution import (
    AllocationLine,
    RefundAllocationError,
    allocation_total,
    validate_allocations,
)
from domain.warehouse.return_order import requires_return
from infrastructure.messaging.idempotency_store import MySQLIdempotencyStore
from infrastructure.messaging.outbox_repository import OutboxRepository
from infrastructure.mock.refund_snapshot_adapter import MockRefundSnapshotAdapter
from infrastructure.payment.mock_refund_channel import (
    ChannelUnknown,
    MockRefundChannelAdapter,
)
from infrastructure.security.confirmation_store import MySQLConfirmationStore

EVENT_PRODUCERS = {
    "refund.execution.requested": "refund-coordinator",
    "refund.execution.succeeded": "payment-adapter",
    "refund.execution.failed": "payment-adapter",
}


class MySqlRefundExecutionService:
    def __init__(
        self,
        engine: Engine,
        channel: MockRefundChannelAdapter,
        outbox: OutboxRepository | None = None,
    ) -> None:
        self._engine = engine
        self._channel = channel
        self._outbox = outbox
        self._confirmations = MySQLConfirmationStore(engine)
        self._idempotency = MySQLIdempotencyStore(engine)

    # ---------- 提交执行 ----------

    def submit(
        self,
        auth: AuthContext,
        refund_request_id: str,
        allocations: list[dict],
        *,
        idempotency_key: str,
        trace: TraceContext,
    ) -> dict:
        self._require_service(auth)
        self._trace_ok(trace)
        try:
            lines = validate_allocations(allocations)
        except RefundAllocationError as exc:
            raise PaymentConflict(str(exc)) from exc
        digest = content_digest({"refund_request_id": refund_request_id, "lines": allocations})
        with self._engine.begin() as connection:
            idem = self._idempotency.bind(connection).reserve(
                auth.tenant_id, auth.subject_id, "refund_execution",
                idempotency_key, digest,
            )
            if idem.status == "succeeded":
                existing = self._load(connection, auth.tenant_id, idem.result_ref)
                if existing is None:
                    raise PaymentConflict("原执行记录待查询")
                return {**self._as_dict(existing), "replayed": True}
            if idem.status != "pending":
                raise PaymentConflict("原执行待核实")
            request = self._load_request(connection, auth, refund_request_id)
            execution_id = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{auth.tenant_id}\x00refund-execution\x00{refund_request_id}"
                f"\x00{idempotency_key}",
            ).hex
            amount_minor = allocation_total(lines)
            resolved = self._validate_execution(
                connection, auth, request, lines, amount_minor
            )
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            connection.execute(text(
                "INSERT INTO refund_execution (tenant_id,execution_id,refund_request_id,"
                " order_id,payment_id,amount_minor,currency,funds_status,idempotency_key,"
                " request_digest,environment,actor_subject_id,customer_id,created_at,"
                " updated_at)"
                " VALUES (:tenant,:execution,:rid,:order,:payment,:amount,:currency,"
                " 'submitting',:idem,:digest,:environment,:actor,:customer,:now,:now)"
            ), {
                "tenant": auth.tenant_id, "execution": execution_id,
                "rid": refund_request_id, "order": request["order_id"],
                "payment": request["payment_id"], "amount": amount_minor,
                "currency": request["currency"], "idem": idempotency_key,
                "digest": digest, "environment": self._channel.environment,
                "actor": auth.subject_id, "customer": request["customer_id"], "now": now,
            })
            for line in lines:
                connection.execute(text(
                    "INSERT INTO refund_allocation (tenant_id,allocation_id,execution_id,"
                    " request_line_id,payment_detail_id,component,amount_minor,environment)"
                    " VALUES (:tenant,:allocation,:execution,:line,:detail,:component,"
                    " :amount,:environment)"
                ), {
                    "tenant": auth.tenant_id,
                    "allocation": uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"{auth.tenant_id}\x00refund-allocation\x00{execution_id}"
                        f"\x00{line.request_line_id}\x00{line.component}",
                    ).hex,
                    "execution": execution_id, "line": line.request_line_id,
                    "detail": resolved[line.request_line_id], "component": line.component,
                    "amount": line.amount_minor, "environment": self._channel.environment,
                })
            self._enqueue(connection, auth.tenant_id, auth.subject_id, trace,
                          "refund.execution.requested",
                          "refund_execution", execution_id,
                          RefundExecutionRequestedPayload(
                              execution_id=execution_id,
                              refund_request_id=refund_request_id,
                              original_payment_id=request["payment_id"],
                              allocation_refs=[line.request_line_id for line in lines],
                              amount_minor=amount_minor,
                              currency=request["currency"],
                              environment=self._channel.environment,
                              actor_subject_id=auth.subject_id,
                              customer_id=request["customer_id"],
                          ))
        # 独立渠道事务：提交原路退款（确定性执行 ID，超时抛 ChannelUnknown）。
        try:
            result = self._channel.execute(
                auth.tenant_id, execution_id, request["order_id"], request["payment_id"],
                [
                    {"payment_detail_id": resolved[line.request_line_id],
                     "component": line.component,
                     "amount_minor": line.amount_minor}
                    for line in lines
                ],
                amount_minor, request["currency"], idempotency_key,
            )
        except ChannelUnknown:
            result = None
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        audit_result: dict = {"amount_minor": amount_minor, "outcome": "unknown"}
        with self._engine.begin() as connection:
            row = self._load(connection, auth.tenant_id, execution_id)
            if row is None or row["funds_status"] != "submitting":
                final = self._load_by_id(auth.tenant_id, execution_id)
                return {**self._as_dict(final), "replayed": True}
            if result is None:
                self._mark(connection, auth.tenant_id, execution_id, "unknown", now)
                audit_result["outcome"] = "unknown_retained"
            elif result.status == "succeeded":
                ledger_id = self._record_success(
                    connection, auth.tenant_id, auth.subject_id, trace, row, result, now
                )
                audit_result.update({"outcome": "succeeded", "ledger_id": ledger_id})
            else:
                connection.execute(text(
                    "UPDATE refund_execution SET funds_status='failed',"
                    " failure_code='CHANNEL_REJECTED', updated_at=:now"
                    " WHERE tenant_id=:tenant AND execution_id=:execution"
                    " AND funds_status='submitting'"
                ), {"now": now, "tenant": auth.tenant_id, "execution": execution_id})
                self._enqueue(connection, auth.tenant_id, auth.subject_id, trace,
                              "refund.execution.failed",
                              "refund_execution", execution_id,
                              RefundExecutionFailedPayload(
                                  execution_id=execution_id,
                                  refund_request_id=row["refund_request_id"],
                                  failure_code="CHANNEL_REJECTED",
                                  retry_decision_ref="manual_review",
                                  source_version="1",
                                  environment=self._channel.environment,
                                  actor_subject_id=auth.subject_id,
                                  customer_id=row["customer_id"],
                              ))
                audit_result["outcome"] = "failed"
            self._idempotency.bind(connection).complete(idem, "succeeded", execution_id)
        self._audit(auth, trace, "refund.execution.submit", execution_id, audit_result)
        final = self._load_by_id(auth.tenant_id, execution_id)
        return {**self._as_dict(final), "replayed": False}

    # ---------- 恢复与回调 ----------

    def recover(self, auth: AuthContext, execution_id: str, *, trace: TraceContext) -> dict:
        self._require_service(auth)
        self._trace_ok(trace)
        row = self._load_by_id(auth.tenant_id, execution_id)
        if row is None:
            raise PaymentConflict("执行记录不存在")
        if row["funds_status"] in ("succeeded", "failed"):
            return {**self._as_dict(row), "replayed": True}
        evidence = self._channel.query(auth.tenant_id, execution_id)
        channel_status = evidence["status"] if evidence is not None else "unknown"
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with self._engine.begin() as connection:
            if channel_status == "succeeded":
                if row["funds_status"] == "unknown":
                    self._mark(connection, auth.tenant_id, execution_id, "processing", now)
                    row = self._load(connection, auth.tenant_id, execution_id)
                result = SimpleNamespace(
                    refund_id=evidence["refund_id"],
                    channel_transaction_id=evidence["source_record_id"],
                    status="succeeded",
                    replayed=True,
                )
                self._record_success(
                    connection, auth.tenant_id, auth.subject_id, trace, row, result, now
                )
            elif channel_status == "failed":
                connection.execute(text(
                    "UPDATE refund_execution SET funds_status='failed',"
                    " failure_code='CHANNEL_REJECTED', updated_at=:now"
                    " WHERE tenant_id=:tenant AND execution_id=:execution"
                    " AND funds_status IN ('submitting','processing','unknown')"
                ), {"now": now, "tenant": auth.tenant_id, "execution": execution_id})
                self._enqueue(connection, auth.tenant_id, auth.subject_id, trace,
                              "refund.execution.failed",
                              "refund_execution", execution_id,
                              RefundExecutionFailedPayload(
                                  execution_id=execution_id,
                                  refund_request_id=row["refund_request_id"],
                                  failure_code="CHANNEL_REJECTED",
                                  retry_decision_ref="manual_review",
                                  source_version="1",
                                  environment=self._channel.environment,
                                  actor_subject_id=auth.subject_id,
                                  customer_id=row["customer_id"],
                              ))
            else:
                connection.execute(text(
                    "UPDATE refund_execution SET funds_status='unknown', updated_at=:now"
                    " WHERE tenant_id=:tenant AND execution_id=:execution"
                    " AND funds_status IN ('submitting','processing')"
                ), {"now": now, "tenant": auth.tenant_id, "execution": execution_id})
        self._audit(auth, trace, "refund.execution.recover", execution_id,
                    {"channel_status": channel_status})
        final = self._load_by_id(auth.tenant_id, execution_id)
        return {**self._as_dict(final), "replayed": False}

    def handle_callback(self, payload: dict, signature: str, *, trace: TraceContext) -> dict:
        tenant_id = payload.get("tenant_id") if isinstance(payload, dict) else None
        if not self._channel.verify_callback(payload, signature) or not isinstance(
            tenant_id, str
        ) or not tenant_id:
            order_hint = payload.get("order_id") if isinstance(payload, dict) else None
            self._record_difference(tenant_id if isinstance(tenant_id, str) else "tenant-north",
                                    None, "callback_mismatch", None, None, 0,
                                    {"reason": "invalid_signature", "order_id": order_hint})
            return {"accepted": False, "reason": "invalid_signature"}
        refund_id = str(payload.get("refund_id", ""))
        row = self._load_by_channel_ref(tenant_id, refund_id)
        if row is None:
            self._record_difference(tenant_id, None, "callback_mismatch", None, refund_id, 0,
                                    {"reason": "unknown_refund"})
            return {"accepted": False, "reason": "unknown_refund"}
        evidence = self._channel.query(tenant_id, row["execution_id"])
        if evidence is None or evidence["status"] != "succeeded":
            # 回调只是线索：无渠道权威成功证据不改资金状态，主动查询核实。
            return {"accepted": False, "reason": "no_authoritative_success",
                    "execution_id": row["execution_id"]}
        if (int(payload.get("amount_minor", -1)) != int(evidence["amount_minor"])
                or payload.get("currency") != evidence["currency"]
                or payload.get("order_id") != evidence["order_id"]):
            self._record_difference(
                tenant_id, None, "callback_mismatch", row["execution_id"], refund_id,
                int(payload.get("amount_minor", 0)),
                {"payload": payload, "channel_amount_minor": int(evidence["amount_minor"])},
            )
            return {"accepted": False, "reason": "mismatch",
                    "execution_id": row["execution_id"]}
        if row["funds_status"] == "succeeded":
            # 重放/乱序回调：成功不回退，幂等无操作。
            return {"accepted": True, "execution_id": row["execution_id"], "noop": True}
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with self._engine.begin() as connection:
            fresh = self._load(connection, tenant_id, row["execution_id"])
            if fresh is None or fresh["funds_status"] == "succeeded":
                return {"accepted": True, "execution_id": row["execution_id"], "noop": True}
            if fresh["funds_status"] == "unknown":
                self._mark(connection, tenant_id, row["execution_id"], "processing", now)
                fresh = self._load(connection, tenant_id, row["execution_id"])
            self._record_success(
                connection, tenant_id, "payment-channel-callback", trace, fresh,
                SimpleNamespace(
                    refund_id=evidence["refund_id"],
                    channel_transaction_id=evidence["source_record_id"],
                    status="succeeded", replayed=True,
                ),
                now,
            )
        self._audit_callback(tenant_id, trace, row["execution_id"])
        return {"accepted": True, "execution_id": row["execution_id"], "noop": False}

    # ---------- 对账 ----------

    def reconcile(
        self,
        auth: AuthContext,
        *,
        merchant_id: str,
        currency: str,
        window_start: datetime,
        window_end: datetime,
        trace: TraceContext,
    ) -> dict:
        self._require_service(auth)
        self._trace_ok(trace)
        flows = self._channel.list_flows(auth.tenant_id, currency, window_start, window_end)
        with self._engine.connect() as connection:
            ledger_rows = connection.execute(text(
                "SELECT channel_transaction_id,amount_minor,execution_id FROM refund_ledger"
                " WHERE tenant_id=:tenant AND currency=:currency"
                " AND confirmed_at >= :start AND confirmed_at < :end"
            ), {"tenant": auth.tenant_id, "currency": currency,
                "start": window_start, "end": window_end}).mappings().all()
        ledger_by_txn = {row["channel_transaction_id"]: dict(row) for row in ledger_rows}
        channel_by_txn = {
            row["source_record_id"]: dict(row) for row in flows if row["source_record_id"]
        }
        run_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{auth.tenant_id}\x00reconcile\x00{merchant_id}\x00{currency}"
            f"\x00{window_start.isoformat()}\x00{window_end.isoformat()}",
        ).hex
        differences: list[dict] = []
        for txn, flow in channel_by_txn.items():
            ledger = ledger_by_txn.get(txn)
            if ledger is None:
                if flow["status"] == "succeeded":
                    differences.append({
                        "type": "channel_only", "channel_ref": flow["refund_id"],
                        "amount_minor": int(flow["amount_minor"]),
                        "detail": {"channel_transaction_id": txn,
                                   "status": flow["status"]},
                    })
                continue
            if int(ledger["amount_minor"]) != int(flow["amount_minor"]):
                differences.append({
                    "type": "amount_mismatch", "channel_ref": flow["refund_id"],
                    "amount_minor": int(flow["amount_minor"]),
                    "detail": {"channel_transaction_id": txn,
                               "ledger_amount_minor": int(ledger["amount_minor"]),
                               "channel_amount_minor": int(flow["amount_minor"]),
                               "execution_id": ledger["execution_id"]},
                })
        for txn, ledger in ledger_by_txn.items():
            if txn not in channel_by_txn:
                differences.append({
                    "type": "ledger_only", "channel_ref": None,
                    "amount_minor": int(ledger["amount_minor"]),
                    "detail": {"channel_transaction_id": txn,
                               "execution_id": ledger["execution_id"]},
                })
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with self._engine.begin() as connection:
            existing = connection.execute(text(
                "SELECT status,difference_count FROM reconciliation_run"
                " WHERE tenant_id=:tenant AND run_id=:run"
            ), {"tenant": auth.tenant_id, "run": run_id}).first()
            if existing is not None:
                return {"run_id": run_id, "status": existing[0],
                        "difference_count": int(existing[1]), "replayed": True}
            succeeded_channel = [
                row for row in channel_by_txn.values() if row["status"] == "succeeded"
            ]
            status = "balanced" if not differences else "difference"
            connection.execute(text(
                "INSERT INTO reconciliation_run (tenant_id,run_id,merchant_id,currency,"
                " window_start,window_end,status,executed_count,executed_minor,"
                " channel_count,channel_minor,difference_count,environment,created_at)"
                " VALUES (:tenant,:run,:merchant,:currency,:start,:end,:status,"
                " :exec_count,:exec_minor,:ch_count,:ch_minor,:diff_count,"
                " :environment,:now)"
            ), {
                "tenant": auth.tenant_id, "run": run_id, "merchant": merchant_id,
                "currency": currency, "start": window_start, "end": window_end,
                "status": status,
                "exec_count": len(ledger_rows),
                "exec_minor": sum(int(r["amount_minor"]) for r in ledger_rows),
                "ch_count": len(succeeded_channel),
                "ch_minor": sum(int(r["amount_minor"]) for r in succeeded_channel),
                "diff_count": len(differences),
                "environment": self._channel.environment, "now": now,
            })
            for item in differences:
                self._write_difference(
                    connection, auth.tenant_id, run_id, item["type"],
                    item["detail"].get("execution_id"),
                    item["channel_ref"], item["amount_minor"], item["detail"],
                )
        self._audit(auth, trace, "refund.reconciliation", run_id,
                    {"status": status, "differences": len(differences)})
        return {"run_id": run_id, "status": status,
                "difference_count": len(differences), "replayed": False}

    # ---------- 查询 ----------

    def query_by_request(self, auth: AuthContext, refund_request_id: str) -> dict:
        verify_tool_context(auth, ("refund:read",))
        with self._engine.connect() as connection:
            request = connection.execute(text(
                "SELECT customer_id,amount_minor,currency FROM refund_request"
                " WHERE tenant_id=:tenant AND refund_request_id=:rid"
            ), {"tenant": auth.tenant_id, "rid": refund_request_id}).mappings().first()
            if request is None:
                raise AuthorizationDenied(
                    ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE
                )
            require_customer_access(auth, request["customer_id"], tenant_id=auth.tenant_id)
            rows = connection.execute(text(
                "SELECT execution_id,funds_status,amount_minor,currency,"
                " channel_transaction_ref,failure_code,environment,created_at"
                " FROM refund_execution WHERE tenant_id=:tenant"
                " AND refund_request_id=:rid ORDER BY created_at,execution_id"
            ), {"tenant": auth.tenant_id, "rid": refund_request_id}).mappings().all()
        executions = [dict(row) for row in rows]
        approved_minor = int(request["amount_minor"])
        succeeded_minor = sum(
            int(row["amount_minor"]) for row in executions
            if row["funds_status"] == "succeeded"
        )
        reserved_minor = sum(
            int(row["amount_minor"]) for row in executions
            if row["funds_status"] in ("submitting", "processing", "unknown")
        )
        if not executions:
            aggregate = "not_executed"
        elif succeeded_minor >= approved_minor:
            aggregate = "succeeded"
        elif any(row["funds_status"] == "unknown" for row in executions):
            aggregate = "unknown"
        elif any(row["funds_status"] in ("submitting", "processing") for row in executions):
            aggregate = "processing"
        else:
            aggregate = "failed"
        return {
            "refund_request_id": refund_request_id,
            "approved_amount_minor": approved_minor,
            "currency": request["currency"],
            "funds_status": aggregate,
            "succeeded_minor": succeeded_minor,
            "reserved_minor": reserved_minor,
            "executions": [
                {
                    "execution_id": row["execution_id"],
                    "funds_status": row["funds_status"],
                    "amount_minor": int(row["amount_minor"]),
                    "currency": row["currency"],
                    "channel_transaction_ref": row["channel_transaction_ref"],
                    "failure_code": row["failure_code"],
                    "environment": row["environment"],
                    "created_at": row["created_at"].isoformat(),
                }
                for row in executions
            ],
        }

    # ---------- 内部工具 ----------

    def _require_service(self, auth: AuthContext) -> None:
        verify_tool_context(auth, ("refund:execute",))
        if PAYMENT_ROLE not in auth.roles:
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)

    @staticmethod
    def _trace_ok(trace: TraceContext) -> None:
        if not isinstance(trace, TraceContext) or not all(
            (trace.request_id, trace.correlation_id, trace.trace_id)
        ):
            raise PaymentConflict("缺少可信链路标识")

    def _load_request(self, connection: Connection, auth: AuthContext, refund_request_id: str):
        request = connection.execute(text(
            "SELECT refund_request_id,order_id,customer_id,reason_code,review_status,"
            " confirmation_id,policy_version,order_version,payment_version,"
            " amount_minor,goods_minor,shipping_minor,currency"
            " FROM refund_request WHERE tenant_id=:tenant"
            " AND refund_request_id=:rid FOR UPDATE"
        ), {"tenant": auth.tenant_id, "rid": refund_request_id}).mappings().first()
        if request is None or request["review_status"] != "approved":
            raise PaymentConflict("退款执行只允许基于已批准的退款申请")
        require_customer_access(auth, request["customer_id"], tenant_id=auth.tenant_id)
        confirmation = self._confirmations.get(auth.tenant_id, request["confirmation_id"])
        if confirmation is None or confirmation.status != "consumed":
            raise PaymentConflict("客户确认未完成，拒绝执行退款")
        resolved = dict(request)
        payment = connection.execute(text(
            "SELECT payment_id FROM mock_pay_payment"
            " WHERE tenant_id=:tenant AND order_id=:order AND status='succeeded'"
            " ORDER BY payment_id LIMIT 1"
        ), {"tenant": auth.tenant_id, "order": request["order_id"]}).first()
        if payment is None:
            raise PaymentConflict("原支付不可用，拒绝执行退款")
        resolved["payment_id"] = payment[0]
        return resolved

    def _validate_execution(
        self,
        connection: Connection,
        auth: AuthContext,
        request: dict,
        lines: list[AllocationLine],
        total_minor: int,
    ) -> dict[str, str]:
        # 三方版本重校（TC-103）：旧批准版本必须重新报价确认审核。
        snapshot = MockRefundSnapshotAdapter(self._engine).bind(connection).load(
            auth.tenant_id, request["order_id"]
        )
        if snapshot is None:
            raise PaymentConflict("订单/支付事实不可用，拒绝执行")
        facts = snapshot.facts
        if (
            facts.policy_version != request["policy_version"]
            or facts.order_version != request["order_version"]
            or facts.payment_version != request["payment_version"]
        ):
            raise PaymentConflict("批准版本已过期，须重新报价确认与审核")
        if facts.currency != request["currency"]:
            raise PaymentConflict("币种与原支付不一致，拒绝执行")
        request_lines = connection.execute(text(
            "SELECT line_id,quantity FROM refund_request_line"
            " WHERE tenant_id=:tenant AND refund_request_id=:rid"
        ), {"tenant": auth.tenant_id, "rid": request["refund_request_id"]})\
            .mappings().all()
        request_line_ids = {row["line_id"] for row in request_lines}
        if any(line.request_line_id not in request_line_ids for line in lines):
            raise PaymentConflict("分摊行超出退款申请范围")
        # 必要退货验收（TC-98 门）：普通退货必须验收完成后才可执行。
        first_qty = int(request_lines[0]["quantity"]) if request_lines else 0
        if requires_return(request["reason_code"], first_qty):
            return_order = connection.execute(text(
                "SELECT status FROM return_order WHERE tenant_id=:tenant"
                " AND refund_request_id=:rid"
            ), {"tenant": auth.tenant_id, "rid": request["refund_request_id"]}).first()
            if return_order is None or return_order[0] != "completed":
                raise PaymentConflict("必要退货验收未完成，拒绝执行退款")
        # 逐行分摊守恒：按支付明细×资金组成累计，成功+在途预留不得超过实付。
        # 分摊行引用申请行；支付明细由服务端按（订单，申请行，原支付）解析。
        line_ids = sorted({line.request_line_id for line in lines})
        placeholders = ", ".join(f":d{i}" for i in range(len(line_ids)))
        line_params = {f"d{i}": value for i, value in enumerate(line_ids)}
        details = connection.execute(text(
            "SELECT payment_detail_id,line_id,paid_goods_minor,paid_shipping_minor"
            " FROM mock_pay_payment_detail WHERE tenant_id=:tenant"
            f" AND order_id=:order AND payment_id=:payment AND line_id IN ({placeholders})"
        ), {"tenant": auth.tenant_id, "order": request["order_id"],
            "payment": request["payment_id"], **line_params}).mappings().all()
        detail_by_line = {row["line_id"]: dict(row) for row in details}
        if len(detail_by_line) != len(line_ids):
            raise PaymentConflict("分摊行未全部对应原支付明细，拒绝执行")
        detail_ids = sorted(row["payment_detail_id"] for row in details)
        placeholders = ", ".join(f":d{i}" for i in range(len(detail_ids)))
        detail_params = {f"d{i}": value for i, value in enumerate(detail_ids)}
        active = connection.execute(text(
            "SELECT a.payment_detail_id,a.component,SUM(a.amount_minor) AS reserved_minor"
            " FROM refund_allocation a JOIN refund_execution e"
            " ON e.tenant_id=a.tenant_id AND e.execution_id=a.execution_id"
            f" WHERE a.tenant_id=:tenant AND a.payment_detail_id IN ({placeholders})"
            " AND e.funds_status IN ('submitting','processing','unknown','succeeded')"
            " GROUP BY a.payment_detail_id,a.component"
        ), {"tenant": auth.tenant_id, **detail_params}).all()
        reserved = {(row[0], row[1]): int(row[2]) for row in active}
        resolved: dict[str, str] = {}
        for line in lines:
            detail = detail_by_line.get(line.request_line_id)
            if detail is None:
                raise PaymentConflict("分摊行未全部对应原支付明细，拒绝执行")
            resolved[line.request_line_id] = detail["payment_detail_id"]
            paid_key = "paid_goods_minor" if line.component == "goods" else "paid_shipping_minor"
            used = reserved.get((detail["payment_detail_id"], line.component), 0)
            if used + line.amount_minor > int(detail[paid_key]):
                raise PaymentConflict("分摊累计超过该明细实付金额")
        succeeded_minor = connection.execute(text(
            "SELECT COALESCE(SUM(amount_minor),0) FROM refund_execution"
            " WHERE tenant_id=:tenant AND refund_request_id=:rid"
            " AND funds_status='succeeded'"
        ), {"tenant": auth.tenant_id, "rid": request["refund_request_id"]}).scalar()
        if int(succeeded_minor) + total_minor > int(request["amount_minor"]):
            raise PaymentConflict("累计退款超过批准金额")
        return resolved

    def _record_success(
        self,
        connection: Connection,
        tenant_id: str,
        actor_subject_id: str,
        trace: TraceContext,
        row,
        result,
        now: datetime,
    ) -> str:
        updated = connection.execute(text(
            "UPDATE refund_execution SET funds_status='succeeded',"
            " channel_ref=:channel_ref, channel_transaction_ref=:txn, updated_at=:now"
            " WHERE tenant_id=:tenant AND execution_id=:execution"
            " AND funds_status IN ('submitting','processing','unknown')"
        ), {
            "channel_ref": result.refund_id, "txn": result.channel_transaction_id,
            "now": now, "tenant": tenant_id, "execution": row["execution_id"],
        })
        if updated.rowcount != 1:
            raise PaymentConflict("执行状态已变化，成功证据未入账")
        ledger_id = uuid.uuid4().hex
        connection.execute(text(
            "INSERT INTO refund_ledger (tenant_id,ledger_id,execution_id,payment_id,"
            " channel_transaction_id,amount_minor,currency,evidence_ref,environment,"
            " confirmed_at,created_at)"
            " VALUES (:tenant,:ledger,:execution,:payment,:txn,:amount,:currency,"
            " :evidence,:environment,:confirmed,:now)"
        ), {
            "tenant": tenant_id, "ledger": ledger_id,
            "execution": row["execution_id"], "payment": row["payment_id"],
            "txn": result.channel_transaction_id, "amount": int(row["amount_minor"]),
            "currency": row["currency"], "evidence": result.refund_id,
            "environment": self._channel.environment, "confirmed": now, "now": now,
        })
        self._enqueue(connection, tenant_id, actor_subject_id, trace,
                      "refund.execution.succeeded", "refund_execution",
                      row["execution_id"],
                      RefundExecutionSucceededPayload(
                          execution_id=row["execution_id"],
                          refund_request_id=row["refund_request_id"],
                          channel_transaction_ref=result.channel_transaction_id,
                          amount_minor=int(row["amount_minor"]),
                          currency=row["currency"],
                          confirmed_at=now.isoformat(),
                          source_version="1",
                          environment=self._channel.environment,
                          actor_subject_id=actor_subject_id,
                          customer_id=row["customer_id"],
                      ))
        return ledger_id

    @staticmethod
    def _mark(connection: Connection, tenant_id: str, execution_id: str, status: str,
              now: datetime) -> None:
        connection.execute(text(
            "UPDATE refund_execution SET funds_status=:status, updated_at=:now"
            " WHERE tenant_id=:tenant AND execution_id=:execution"
        ), {"status": status, "now": now, "tenant": tenant_id, "execution": execution_id})

    def _load(self, connection: Connection, tenant_id: str, execution_id: str):
        return connection.execute(text(
            "SELECT execution_id,refund_request_id,order_id,payment_id,amount_minor,"
            " currency,funds_status,channel_ref,channel_transaction_ref,failure_code,"
            " environment,actor_subject_id,customer_id"
            " FROM refund_execution WHERE tenant_id=:tenant AND execution_id=:execution"
            " FOR UPDATE"
        ), {"tenant": tenant_id, "execution": execution_id}).mappings().first()

    def _load_by_id(self, tenant_id: str, execution_id: str):
        with self._engine.connect() as connection:
            return self._load(connection, tenant_id, execution_id)

    def _load_by_channel_ref(self, tenant_id: str, refund_id: str):
        with self._engine.connect() as connection:
            row = connection.execute(text(
                "SELECT execution_id,refund_request_id,order_id,payment_id,amount_minor,"
                " currency,funds_status,channel_ref,channel_transaction_ref,failure_code,"
                " environment,actor_subject_id,customer_id"
                " FROM refund_execution WHERE tenant_id=:tenant AND channel_ref=:refund"
            ), {"tenant": tenant_id, "refund": refund_id}).mappings().first()
        return dict(row) if row is not None else None

    def _record_difference(
        self, tenant_id: str, run_id: str | None, difference_type: str,
        execution_id: str | None, channel_ref: str | None, amount_minor: int,
        detail: dict,
    ) -> None:
        with self._engine.begin() as connection:
            self._write_difference(
                connection, tenant_id, run_id, difference_type,
                execution_id, channel_ref, amount_minor, detail,
            )

    def _write_difference(
        self,
        connection: Connection,
        tenant_id: str,
        run_id: str | None,
        difference_type: str,
        execution_id: str | None,
        channel_ref: str | None,
        amount_minor: int,
        detail: dict,
    ) -> None:
        connection.execute(text(
            "INSERT INTO reconciliation_difference (tenant_id,difference_id,run_id,"
            " difference_type,execution_id,channel_ref,amount_minor,detail,"
            " environment,created_at)"
            " VALUES (:tenant,:difference,:run,:type,:execution,:channel,:amount,"
            " :detail,:environment,:now)"
        ), {
            "tenant": tenant_id, "difference": uuid.uuid4().hex, "run": run_id,
            "type": difference_type, "execution": execution_id,
            "channel": channel_ref, "amount": amount_minor,
            "detail": json.dumps(detail, ensure_ascii=False, default=str),
            "environment": self._channel.environment,
            "now": datetime.now(timezone.utc).replace(tzinfo=None),
        })

    def _enqueue(
        self,
        connection: Connection,
        tenant_id: str,
        actor_subject_id: str,
        trace: TraceContext,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload,
    ) -> None:
        if self._outbox is None:
            raise PaymentConflict("事件出口未装配，操作失败关闭")
        event = EventEnvelope(
            schema_version="1.0",
            event_id=uuid.uuid4().hex,
            event_type=event_type,
            event_version=1,
            tenant_id=tenant_id,
            request_id=trace.request_id,
            correlation_id=trace.correlation_id,
            trace_id=trace.trace_id,
            operation_id=uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{tenant_id}:{aggregate_type}:{aggregate_id}:{event_type}:"
                f"{trace.request_id}",
            ).hex,
            causation_id=None,
            parent_event_id=None,
            producer=EVENT_PRODUCERS[event_type],
            occurred_at=datetime.now(timezone.utc).isoformat(),
            idempotency_key=f"{event_type}:{aggregate_id}:{trace.request_id}",
            subject_id=actor_subject_id,
            retry_count=0,
            payload=payload,
        )
        self._outbox.enqueue(
            connection, event, aggregate_type=aggregate_type, aggregate_id=aggregate_id,
            thread_id=trace.thread_id,
        )

    def _audit(self, auth, trace: TraceContext, action: str, resource_id: str,
               result: dict) -> None:
        self._write_audit(auth.tenant_id, auth.subject_id, trace, action, resource_id, result)

    def _audit_callback(self, tenant_id: str, trace: TraceContext,
                        execution_id: str) -> None:
        self._write_audit(tenant_id, "payment-channel-callback", trace,
                          "refund.execution.callback", execution_id,
                          {"accepted": True})

    def _write_audit(self, tenant_id: str, subject_id: str, trace: TraceContext,
                     action: str, resource_id: str, result: dict) -> None:
        with self._engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO agent_audit_log (audit_id,tenant_id,request_id,correlation_id,"
                " trace_id,thread_id,actor_subject_id,customer_id,action,resource_refs,"
                " parameter_summary,result_summary,status)"
                " VALUES (:audit_id,:tenant,:request,:correlation,:trace,:thread,:actor,"
                " NULL,:action,:refs,:params,:result,'success')"
            ), {
                "audit_id": uuid.uuid4().hex, "tenant": tenant_id,
                "request": trace.request_id, "correlation": trace.correlation_id,
                "trace": trace.trace_id, "thread": trace.thread_id,
                "actor": subject_id, "action": action,
                "refs": json.dumps([f"refund_execution:{resource_id}"]),
                "params": json.dumps({}),
                "result": json.dumps(result, ensure_ascii=False, default=str),
            })

    @staticmethod
    def _as_dict(row) -> dict:
        return {
            "execution_id": row["execution_id"],
            "refund_request_id": row["refund_request_id"],
            "order_id": row["order_id"],
            "payment_id": row["payment_id"],
            "amount_minor": int(row["amount_minor"]),
            "currency": row["currency"],
            "funds_status": row["funds_status"],
            "channel_ref": row["channel_ref"],
            "channel_transaction_ref": row["channel_transaction_ref"],
            "failure_code": row["failure_code"],
            "environment": row["environment"],
            "customer_id": row["customer_id"],
        }
