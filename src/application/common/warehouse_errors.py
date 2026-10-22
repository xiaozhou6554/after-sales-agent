"""仓储退货验收冲突：调用方映射为 STATE_CONFLICT/409。"""

from __future__ import annotations


class WarehouseConflict(Exception):
    """收货/验收/登记违反守恒、版本或状态约束。"""
