"""OrderQueryPort：企业订单/支付只读查询端口（技术详细规格 8.2 端口清单）。

- 实现方：MockOrderAdapter（开发测试）、EnterpriseOrderAdapter（生产接入）；
- 租户过滤是实现方的硬性义务：所有查询必须携带 tenant_id 条件；
- 找不到资源返回 None，不抛异常；依赖失败抛 OrderDependencyError。
"""

from __future__ import annotations

from typing import Protocol

from domain.order.snapshots import OrderSnapshot, PaymentSnapshot


class OrderDependencyError(RuntimeError):
    """订单/支付数据源连接或查询失败；属瞬态错误，可进入只读重试预算。"""


class OrderQueryPort(Protocol):
    def query_order(self, tenant_id: str, order_id: str) -> OrderSnapshot | None:
        """按租户+订单号读取订单快照；不存在返回 None。"""
        ...

    def query_payment(self, tenant_id: str, order_id: str) -> PaymentSnapshot | None:
        """按租户+订单号读取支付执行快照；订单存在但无支付记录返回 None（支付未知）。"""
        ...
