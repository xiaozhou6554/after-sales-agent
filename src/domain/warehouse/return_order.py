"""FR-21 退货单聚合：数量守恒与状态机，纯 Python，不依赖任何外部库。"""

from __future__ import annotations

from dataclasses import dataclass, replace


class WarehouseStateError(ValueError):
    """收货/验收违反守恒或状态约束；调用方转为 STATE_CONFLICT。"""


REASON_CATEGORIES = {
    "no_longer_needed": "ordinary_return",
    "changed_mind": "ordinary_return",
    "quality_issue": "quality_dispute",
}


def requires_return(reason_code: str, approved_goods_qty: int) -> bool:
    """普通退货需原物返还；质量争议按政策仅退款，凭批准依据执行。"""
    return REASON_CATEGORIES.get(reason_code) == "ordinary_return" and approved_goods_qty > 0


@dataclass(frozen=True)
class ReturnLine:
    line_id: str
    order_line_id: str
    rma_id: str
    approved_qty: int
    received_qty: int = 0
    accepted_qty: int = 0
    rejected_qty: int = 0


@dataclass(frozen=True)
class ReturnOrder:
    return_id: str
    refund_request_id: str
    order_id: str
    customer_id: str
    status: str
    revision: int
    environment: str
    lines: tuple[ReturnLine, ...]

    def record_receipt(self, line_id: str, received_qty: int) -> "ReturnOrder":
        if received_qty <= 0:
            raise WarehouseStateError("收货数量必须为正")
        line = self._line(line_id)
        if line.received_qty + received_qty > line.approved_qty:
            raise WarehouseStateError("收货数量超出批准数量，禁止超收")
        lines = tuple(
            replace(item, received_qty=item.received_qty + received_qty)
            if item.line_id == line_id
            else item
            for item in self.lines
        )
        return replace(self, lines=lines, status=self._receipt_status(lines))

    def record_inspection(
        self, line_id: str, accepted_qty: int, rejected_qty: int
    ) -> "ReturnOrder":
        if accepted_qty < 0 or rejected_qty < 0 or accepted_qty + rejected_qty == 0:
            raise WarehouseStateError("质检数量必须为非负且合计为正")
        line = self._line(line_id)
        uninspected = line.received_qty - line.accepted_qty - line.rejected_qty
        if accepted_qty + rejected_qty > uninspected:
            raise WarehouseStateError("质检数量超出未检实收数量，禁止重复累计")
        lines = tuple(
            replace(
                item,
                accepted_qty=item.accepted_qty + accepted_qty,
                rejected_qty=item.rejected_qty + rejected_qty,
            )
            if item.line_id == line_id
            else item
            for item in self.lines
        )
        return replace(self, lines=lines, status=self._inspection_status(lines))

    def _line(self, line_id: str) -> ReturnLine:
        for item in self.lines:
            if item.line_id == line_id:
                return item
        raise WarehouseStateError("退货单行不存在")

    @staticmethod
    def _receipt_status(lines: tuple[ReturnLine, ...]) -> str:
        if all(item.received_qty == item.approved_qty for item in lines):
            return "received"
        if any(item.received_qty > 0 for item in lines):
            return "partially_received"
        return "open"

    @staticmethod
    def _inspection_status(lines: tuple[ReturnLine, ...]) -> str:
        if all(
            item.accepted_qty + item.rejected_qty == item.received_qty for item in lines
        ):
            return "completed"
        return "partially_inspected"
