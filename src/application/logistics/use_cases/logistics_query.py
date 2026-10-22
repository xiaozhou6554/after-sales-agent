"""物流工具用例：先确认订单归属，再返回物流事实。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from application.common.authorization import (
    GENERIC_DENIED_MESSAGE,
    AuthorizationDenied,
    require_scope,
)
from application.common.context import AuthContext
from application.common.errors import ErrorCode
from application.logistics.ports.logistics_query import LogisticsQueryPort
from application.order.use_cases.order_query import QueryOrderUseCase
from domain.logistics.policies import delivery_exceptions
from domain.logistics.snapshots import LogisticsSnapshot


class QueryLogisticsUseCase:
    def __init__(
        self,
        orders: QueryOrderUseCase,
        logistics: LogisticsQueryPort,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._orders = orders
        self._logistics = logistics
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def source_version(self) -> str:
        return self._logistics.source_version

    def by_order(self, auth: AuthContext, order_id: str) -> tuple[LogisticsSnapshot, ...]:
        require_scope(auth, "logistics:read")
        self._orders.execute(auth, order_id)
        return self._logistics.by_order(auth.tenant_id, order_id)

    def by_tracking(self, auth: AuthContext, tracking_number: str) -> LogisticsSnapshot:
        require_scope(auth, "logistics:read")
        shipment = self._logistics.by_tracking(auth.tenant_id, tracking_number)
        if shipment is None:
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
        self._orders.execute(auth, shipment.order_id)
        return shipment

    def query(
        self, auth: AuthContext, order_id: str | None, tracking_number: str | None
    ) -> tuple[str | None, tuple[LogisticsSnapshot, ...]]:
        if not order_id and not tracking_number:
            raise AuthorizationDenied(ErrorCode.INVALID_ARGUMENT, "需要订单号或物流单号")
        if tracking_number:
            shipment = self.by_tracking(auth, tracking_number)
            if order_id and shipment.order_id != order_id:
                raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
            return shipment.order_id, (shipment,)
        assert order_id is not None
        return order_id, self.by_order(auth, order_id)

    def check_exception(self, auth: AuthContext, order_id: str) -> tuple[dict, ...]:
        shipments = self.by_order(auth, order_id)
        return tuple(
            {
                "exception_code": item.exception_code,
                "description": item.description,
                "detected_at": item.detected_at.isoformat(),
                "evidence_refs": list(item.evidence_refs),
            }
            for shipment in shipments
            for item in delivery_exceptions(shipment, self._clock())
        )
