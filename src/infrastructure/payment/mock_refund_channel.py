"""独立支付渠道模拟适配器：只写 mock_pay_* 源表，与售后库不共用写事务。

FR-22/28.1：支付渠道是独立系统；本适配器以独立事务模拟原路退款，
幂等键派生确定性退款单与渠道流水号，重放返回原记录；超时脚本注入
只影响提交结果，不产生渠道事实。生产替换为企业渠道适配器（未验证）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine

ENVIRONMENT = "simulation"
CALLBACK_SECRET = "mock-channel-callback-secret"


class ChannelUnknown(Exception):
    """提交超时且渠道状态未知；调用方只能查询，不得当失败处理。"""


@dataclass(frozen=True)
class ChannelRefundResult:
    refund_id: str
    channel_transaction_id: str
    status: str
    replayed: bool


def refund_id_for(tenant_id: str, execution_id: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"{tenant_id}\x00pay-refund\x00{execution_id}").hex


def transaction_id_for(tenant_id: str, execution_id: str) -> str:
    return uuid.uuid5(
        uuid.NAMESPACE_URL, f"{tenant_id}\x00pay-channel-txn\x00{execution_id}"
    ).hex


def callback_signature(payload: dict, secret: str = CALLBACK_SECRET) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


class MockRefundChannelAdapter:
    environment = ENVIRONMENT

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._scripts: dict[str, str] = {}

    def set_script(self, idempotency_key: str, outcome: str) -> None:
        """测试注入：succeeded/failed/timeout，按提交幂等键；未注入默认渠道成功。"""
        self._scripts[idempotency_key] = outcome

    def execute(
        self,
        tenant_id: str,
        execution_id: str,
        order_id: str,
        payment_id: str,
        details: list[dict],
        amount_minor: int,
        currency: str,
        idempotency_key: str,
    ) -> ChannelRefundResult:
        refund_id = refund_id_for(tenant_id, execution_id)
        txn_id = transaction_id_for(tenant_id, execution_id)
        with self._engine.begin() as connection:
            row = connection.execute(text(
                "SELECT amount_minor,currency,status FROM mock_pay_refund_execution"
                " WHERE tenant_id=:tenant AND refund_id=:refund"
            ), {"tenant": tenant_id, "refund": refund_id}).first()
            if row is not None:
                if (int(row[0]), row[1]) != (amount_minor, currency):
                    raise ValueError("同执行 ID 的渠道退款金额/币种不一致")
                return ChannelRefundResult(refund_id, txn_id, row[2], True)
            outcome = self._scripts.get(idempotency_key, "succeeded")
            if outcome == "timeout":
                raise ChannelUnknown("渠道提交超时，结果未知")
            status = "succeeded" if outcome == "succeeded" else "failed"
            connection.execute(text(
                "INSERT INTO mock_pay_refund_execution"
                " (tenant_id,refund_id,order_id,payment_id,amount_minor,currency,status,"
                " idempotency_key,source_record_id,environment,created_at)"
                " VALUES (:tenant,:refund,:order,:payment,:amount,:currency,:status,"
                " :idem,:source,:environment,:created_at)"
            ), {
                "tenant": tenant_id, "refund": refund_id, "order": order_id,
                "payment": payment_id, "amount": amount_minor, "currency": currency,
                "status": status, "idem": idempotency_key[:128], "source": txn_id,
                "environment": self.environment,
                "created_at": datetime.now(timezone.utc).replace(tzinfo=None),
            })
            for detail in details:
                connection.execute(text(
                    "INSERT INTO mock_pay_refund_detail"
                    " (tenant_id,refund_detail_id,payment_detail_id,refund_id,status,"
                    " refunded_goods_minor,currency,source_record_id)"
                    " VALUES (:tenant,:detail_id,:payment_detail,:refund,:status,"
                    " :goods,:currency,:source)"
                ), {
                    "tenant": tenant_id,
                    "detail_id": uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"{tenant_id}\x00pay-refund-detail\x00{refund_id}"
                        f"\x00{detail['payment_detail_id']}\x00{detail['component']}",
                    ).hex,
                    "payment_detail": detail["payment_detail_id"], "refund": refund_id,
                    "status": status,
                    "goods": detail["amount_minor"] if detail["component"] == "goods" else 0,
                    "currency": currency, "source": txn_id,
                })
        return ChannelRefundResult(refund_id, txn_id, status, False)

    def query(self, tenant_id: str, execution_id: str) -> dict | None:
        """渠道权威状态查询；无记录视为结果未知。"""
        refund_id = refund_id_for(tenant_id, execution_id)
        with self._engine.connect() as connection:
            row = connection.execute(text(
                "SELECT refund_id,amount_minor,currency,status,source_record_id,order_id,"
                " payment_id FROM mock_pay_refund_execution"
                " WHERE tenant_id=:tenant AND refund_id=:refund"
            ), {"tenant": tenant_id, "refund": refund_id}).mappings().first()
        return dict(row) if row is not None else None

    def list_flows(
        self, tenant_id: str, currency: str, start: datetime, end: datetime
    ) -> list[dict]:
        with self._engine.connect() as connection:
            rows = connection.execute(text(
                "SELECT refund_id,order_id,payment_id,amount_minor,status,"
                " source_record_id,created_at FROM mock_pay_refund_execution"
                " WHERE tenant_id=:tenant AND currency=:currency"
                " AND created_at IS NOT NULL AND created_at >= :start AND created_at < :end"
            ), {"tenant": tenant_id, "currency": currency, "start": start, "end": end})\
                .mappings().all()
        return [dict(row) for row in rows]

    def verify_callback(self, payload: dict, signature: str) -> bool:
        if not isinstance(payload, dict) or not isinstance(signature, str):
            return False
        expected = callback_signature(payload)
        return hmac.compare_digest(expected, signature)
