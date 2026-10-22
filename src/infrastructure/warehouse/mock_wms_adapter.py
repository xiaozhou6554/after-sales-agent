"""独立 WMS 模拟适配器：只写 mock_wms_* 源表，与售后库不共用写事务。

FR-21/28.1：WMS 是独立系统；本适配器以独立连接和事务模拟跨系统调用，
幂等键派生确定性源记录 ID，重放返回原记录，不产生重复收货。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Engine

ENVIRONMENT = "simulation"


@dataclass(frozen=True)
class WmsLineRegistration:
    order_line_id: str
    rma_id: str
    source_version: str


@dataclass(frozen=True)
class WmsReceiptRecord:
    receipt_id: str
    source_version: str
    replayed: bool


@dataclass(frozen=True)
class WmsInspectionRecord:
    inspection_id: str
    source_version: str
    replayed: bool


def rma_id_for(tenant_id: str, return_id: str, order_line_id: str) -> str:
    return uuid.uuid5(
        uuid.NAMESPACE_URL, f"{tenant_id}\x00wms-rma\x00{return_id}\x00{order_line_id}"
    ).hex


class MockWarehouseReturnAdapter:
    """WarehouseReturnPort 的模拟实现；生产替换为企业 WMS HTTP 适配器（未验证）。"""

    environment = ENVIRONMENT

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def create_return(
        self,
        tenant_id: str,
        return_id: str,
        lines: list[dict],
        idempotency_key: str,
    ) -> list[WmsLineRegistration]:
        registrations = []
        with self._engine.begin() as connection:
            for line in lines:
                rma_id = rma_id_for(tenant_id, return_id, line["order_line_id"])
                row = connection.execute(text(
                    "SELECT authorized_quantity FROM mock_wms_return_authorization"
                    " WHERE tenant_id=:tenant AND rma_id=:rma"
                ), {"tenant": tenant_id, "rma": rma_id}).first()
                if row is None:
                    connection.execute(text(
                        "INSERT INTO mock_wms_return_authorization"
                        " (tenant_id,rma_id,order_id,line_id,authorized_quantity,status)"
                        " VALUES (:tenant,:rma,:order,:line,:qty,'authorized')"
                    ), {
                        "tenant": tenant_id, "rma": rma_id, "order": line["order_id"],
                        "line": line["order_line_id"], "qty": line["approved_qty"],
                    })
                elif int(row[0]) != line["approved_qty"]:
                    raise ValueError("WMS 退货授权数量与原登记不一致")
                registrations.append(
                    WmsLineRegistration(line["order_line_id"], rma_id, "1")
                )
        return registrations

    def record_receipt(
        self,
        tenant_id: str,
        rma_id: str,
        received_qty: int,
        idempotency_key: str,
    ) -> WmsReceiptRecord:
        receipt_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{tenant_id}\x00wms-receipt\x00{rma_id}\x00{idempotency_key}",
        ).hex
        with self._engine.begin() as connection:
            row = connection.execute(text(
                "SELECT received_quantity FROM mock_wms_receipt"
                " WHERE tenant_id=:tenant AND receipt_id=:receipt"
            ), {"tenant": tenant_id, "receipt": receipt_id}).first()
            if row is not None:
                if int(row[0]) != received_qty:
                    raise ValueError("同幂等键的 WMS 收货数量不一致")
                return WmsReceiptRecord(receipt_id, "1", True)
            connection.execute(text(
                "INSERT INTO mock_wms_receipt"
                " (tenant_id,receipt_id,rma_id,received_quantity,inspected_quantity,"
                " source_record_id)"
                " VALUES (:tenant,:receipt,:rma,:qty,0,:source)"
            ), {
                "tenant": tenant_id, "receipt": receipt_id, "rma": rma_id,
                "qty": received_qty, "source": idempotency_key[:128],
            })
        return WmsReceiptRecord(receipt_id, "1", False)

    def record_inspection(
        self,
        tenant_id: str,
        rma_id: str,
        receipt_id: str,
        accepted_qty: int,
        rejected_qty: int,
        reason_codes: list[str],
        idempotency_key: str,
    ) -> WmsInspectionRecord:
        inspection_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{tenant_id}\x00wms-inspection\x00{receipt_id}\x00{idempotency_key}",
        ).hex
        with self._engine.begin() as connection:
            row = connection.execute(text(
                "SELECT passed_quantity,rejected_quantity FROM mock_wms_inspection"
                " WHERE tenant_id=:tenant AND inspection_id=:inspection"
            ), {"tenant": tenant_id, "inspection": inspection_id}).first()
            if row is not None:
                if (int(row[0]), int(row[1])) != (accepted_qty, rejected_qty):
                    raise ValueError("同幂等键的 WMS 质检数量不一致")
                return WmsInspectionRecord(inspection_id, "1", True)
            connection.execute(text(
                "INSERT INTO mock_wms_inspection"
                " (tenant_id,inspection_id,receipt_id,passed_quantity,rejected_quantity,"
                " reason_code,confirmed_by)"
                " VALUES (:tenant,:inspection,:receipt,:passed,:rejected,:reason,'wms-mock')"
            ), {
                "tenant": tenant_id, "inspection": inspection_id, "receipt": receipt_id,
                "passed": accepted_qty, "rejected": rejected_qty,
                "reason": json.dumps(reason_codes)[:64],
            })
            connection.execute(text(
                "UPDATE mock_wms_receipt SET inspected_quantity=inspected_quantity+:qty"
                " WHERE tenant_id=:tenant AND receipt_id=:receipt"
            ), {"tenant": tenant_id, "receipt": receipt_id, "qty": accepted_qty + rejected_qty})
        return WmsInspectionRecord(inspection_id, "1", False)
