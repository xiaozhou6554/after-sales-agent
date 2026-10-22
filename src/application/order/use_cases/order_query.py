"""订单/支付查询用例：服务端归属校验（ownership_guard 语义）的定点。

- 租户过滤由端口实现方执行；本用例在此之上校验客户归属；
- 归属不成立与订单不存在返回同一个泛化 RESOURCE_UNAVAILABLE（26.8），
  不泄露订单存在性；跨用户订单与不存在订单对外不可区分；
- OrderDependencyError 原样上抛，由接口层映射依赖失败信封并进入只读重试预算。
"""

from __future__ import annotations

from application.common.authorization import (
    GENERIC_DENIED_MESSAGE,
    AuthorizationDenied,
    require_customer_access,
)
from application.common.context import AuthContext
from application.common.errors import ErrorCode
from application.order.dto.order import PaymentQueryResult, payment_result
from application.order.ports.order_query import OrderQueryPort
from domain.order.snapshots import OrderSnapshot


def _deny_absent() -> AuthorizationDenied:
    """不存在与越权共用同一泛化拒绝，避免泄露订单存在性。"""
    return AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)


class QueryOrderUseCase:
    def __init__(self, port: OrderQueryPort) -> None:
        self._port = port

    def execute(self, auth: AuthContext, order_id: str) -> OrderSnapshot:
        """先读后判：读不到与读到但无归属走同一拒绝语义。"""
        snapshot = self._port.query_order(auth.tenant_id, order_id)
        if snapshot is None:
            raise _deny_absent()
        require_customer_access(auth, snapshot.owner_customer_id)
        return snapshot


class QueryPaymentUseCase:
    def __init__(self, port: OrderQueryPort) -> None:
        self._port = port

    def execute(self, auth: AuthContext, order_id: str) -> PaymentQueryResult:
        """支付查询先确认订单归属；订单在但无支付记录返回 unknown（支付未知）。"""
        order = self._port.query_order(auth.tenant_id, order_id)
        if order is None:
            raise _deny_absent()
        require_customer_access(auth, order.owner_customer_id)
        return payment_result(self._port.query_payment(auth.tenant_id, order_id), order)
