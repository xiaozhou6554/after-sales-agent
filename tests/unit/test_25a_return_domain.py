"""退货单聚合纯域规则：数量守恒、状态机与退货必要性政策。"""

import pytest

from domain.warehouse.return_order import (
    ReturnLine,
    ReturnOrder,
    WarehouseStateError,
    requires_return,
)


def _order(**overrides) -> ReturnOrder:
    lines = overrides.pop(
        "lines",
        (
            ("RL-1", "OL-1", 2),
            ("RL-2", "OL-2", 1),
        ),
    )
    base = dict(
        return_id="RET-1",
        refund_request_id="RR-1",
        order_id="ORDER-1",
        customer_id="CUSTOMER-01",
        status="open",
        revision=0,
        environment="simulation",
    )
    base.update(overrides)
    return ReturnOrder(
        lines=tuple(
            ReturnLine(line_id, order_line_id, f"RMA-{line_id}", approved_qty)
            for line_id, order_line_id, approved_qty in lines
        ),
        **base,
    )


def test_requires_return_policy_gates_by_reason_and_goods_qty() -> None:
    assert requires_return("no_longer_needed", 2)
    assert requires_return("changed_mind", 1)
    assert not requires_return("quality_issue", 3)
    assert not requires_return("no_longer_needed", 0)
    assert not requires_return("unknown_reason", 2)


def test_partial_then_full_receipt_transitions_status() -> None:
    order = _order().record_receipt("RL-1", 1)
    assert order.status == "partially_received"
    assert order.lines[0].received_qty == 1
    order = order.record_receipt("RL-1", 1).record_receipt("RL-2", 1)
    assert order.status == "received"


@pytest.mark.parametrize(
    ("line_id", "qty"),
    [("RL-1", 0), ("RL-1", -1), ("RL-9", 1)],
)
def test_receipt_rejects_non_positive_or_unknown_line(line_id: str, qty: int) -> None:
    with pytest.raises(WarehouseStateError):
        _order().record_receipt(line_id, qty)


def test_receipt_rejects_over_receipt_beyond_approved_qty() -> None:
    with pytest.raises(WarehouseStateError, match="超收"):
        _order().record_receipt("RL-1", 3)


def test_inspection_respects_uninspected_received_qty() -> None:
    order = _order().record_receipt("RL-1", 2)
    order = order.record_inspection("RL-1", 1, 0)
    assert order.status == "partially_inspected"
    with pytest.raises(WarehouseStateError, match="未检实收"):
        order.record_inspection("RL-1", 2, 0)
    order = order.record_inspection("RL-1", 0, 1)
    assert order.status == "completed"


def test_inspection_completed_when_all_received_qty_inspected() -> None:
    order = _order().record_receipt("RL-1", 2).record_receipt("RL-2", 1)
    order = order.record_inspection("RL-1", 2, 0).record_inspection("RL-2", 1, 0)
    assert order.status == "completed"


@pytest.mark.parametrize(
    ("accepted", "rejected"),
    [(0, 0), (-1, 1), (1, -1)],
)
def test_inspection_rejects_invalid_qty(accepted: int, rejected: int) -> None:
    order = _order().record_receipt("RL-1", 2)
    with pytest.raises(WarehouseStateError):
        order.record_inspection("RL-1", accepted, rejected)
