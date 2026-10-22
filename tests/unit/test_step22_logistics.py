"""步骤22：轨迹归属、确定性异常和未知预计送达时间。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from application.agent.guard import evaluate_envelope
from application.common.authorization import AuthorizationDenied
from application.common.context import DataScope
from application.logistics.use_cases.logistics_query import QueryLogisticsUseCase
from application.order.use_cases.order_query import QueryOrderUseCase
from domain.logistics.policies import delivery_exceptions
from infrastructure.mock.logistics_adapter import MockLogisticsAdapter
from tests.unit.test_step09_order_use_case import FakeOrderPort, make_auth, make_snapshot

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "data" / "mock" / "v2" / "logistics.json"
NOW = datetime(2026, 10, 19, 4, tzinfo=timezone.utc)


def _case():
    orders = FakeOrderPort({("tenant-north", "ORDER-001"): make_snapshot()}, {})
    return QueryLogisticsUseCase(
        QueryOrderUseCase(orders), MockLogisticsAdapter(FIXTURE), lambda: NOW
    )


def _auth():
    return make_auth(
        scopes=("order:read", "logistics:read"),
        expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )


def test_tracking_requires_authorized_order() -> None:
    case = _case()
    auth = _auth()
    shipment = case.by_tracking(auth, "MOCK-TRACK-N-001")
    assert shipment.order_id == "ORDER-001"
    assert shipment.estimated_delivery_at is None
    with pytest.raises(AuthorizationDenied):
        case.by_tracking(auth, "MOCK-TRACK-N-002")
    with pytest.raises(AuthorizationDenied):
        case.by_tracking(auth, "NONEXISTENT")


def test_order_and_tracking_mismatch_is_rejected() -> None:
    with pytest.raises(AuthorizationDenied):
        _case().query(_auth(), "ORDER-002", "MOCK-TRACK-N-001")


def test_explicit_exception_and_stale_rule() -> None:
    source = MockLogisticsAdapter(FIXTURE)
    damaged = source.by_tracking("tenant-north", "MOCK-TRACK-N-002")
    assert damaged is not None
    assert delivery_exceptions(damaged, NOW)[0].exception_code == "PACKAGE_DAMAGED"
    delivered = source.by_tracking("tenant-north", "MOCK-TRACK-N-001")
    assert delivered is not None
    assert delivery_exceptions(delivered, NOW) == ()
    in_transit = replace(delivered, status="in_transit", exception_code=None)
    after = in_transit.events[-1].time + timedelta(hours=72)
    assert delivery_exceptions(in_transit, after)[0].exception_code == "TRACKING_STALE"
    assert delivery_exceptions(in_transit, after - timedelta(seconds=1)) == ()


def test_non_terminal_source_fact_preserves_estimated_time() -> None:
    source = MockLogisticsAdapter(FIXTURE)
    shipment = source.by_tracking("tenant-north", "MOCK-TRACK-N-003")
    assert shipment is not None
    assert shipment.status == "in_transit"
    assert shipment.estimated_delivery_at is not None
    assert delivery_exceptions(shipment, NOW) == ()

    orders = FakeOrderPort(
        {
            ("tenant-north", "ORDER-003"): replace(
                make_snapshot(), order_id="ORDER-003", owner_customer_id="CUSTOMER-03"
            )
        },
        {},
    )
    auth = replace(_auth(), data_scope=DataScope(customer_ids=("CUSTOMER-03",)))
    queried = QueryLogisticsUseCase(QueryOrderUseCase(orders), source, lambda: NOW).by_tracking(
        auth, "MOCK-TRACK-N-003"
    )
    assert queried.estimated_delivery_at == shipment.estimated_delivery_at


def test_logistics_answer_summary_only_uses_authoritative_eta() -> None:
    shipment = {
        "tracking_number": "MOCK-TRACK-N-003",
        "status": "in_transit",
        "events": [{"time": "2026-10-16T08:00:00Z", "status": "in_transit"}],
        "exception_code": None,
        "estimated_delivery_at": None,
    }
    envelope = {"status": "success", "data": {"order_id": "ORDER-003", "shipments": [shipment]}}
    unknown = evaluate_envelope("query_logistics", envelope)
    assert "MOCK-TRACK-N-003" in unknown.summary
    assert "预计送达" not in unknown.summary

    shipment["estimated_delivery_at"] = "2026-10-22T18:00:00Z"
    known = evaluate_envelope("query_logistics", envelope)
    assert "物流源预计送达 2026-10-22T18:00:00Z" in known.summary
