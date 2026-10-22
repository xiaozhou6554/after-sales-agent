"""收货地址值对象与资格判定（FR-18）：明文仅在受控表单与会话内出现。"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PHONE_RE = re.compile(r"^1[3-9]\d{9}$")

# 本地 mock 的配送范围；企业接入时替换为 OMS 配送范围服务。
DELIVERY_SUPPORTED_PROVINCES = frozenset(
    {"北京市", "上海市", "广东省", "浙江省", "江苏省", "四川省"}
)

DENIAL_ORDER_NOT_PENDING_SHIPMENT = "ORDER_NOT_PAID_PENDING_SHIPMENT"
DENIAL_PAYMENT_NOT_SUCCEEDED = "PAYMENT_NOT_SUCCEEDED"
DENIAL_ACTIVE_REFUND_EXISTS = "ORDER_HAS_ACTIVE_REFUND"
DENIAL_DELIVERY_OUT_OF_RANGE = "ADDRESS_OUT_OF_DELIVERY_RANGE"


@dataclass(frozen=True)
class ShippingAddress:
    receiver_name: str
    contact_phone: str
    province: str
    city: str
    district: str
    detail_address: str

    def validate(self) -> None:
        if not (2 <= len(self.receiver_name) <= 20):
            raise ValueError("收件人姓名长度须在2到20字之间")
        if not _PHONE_RE.fullmatch(self.contact_phone):
            raise ValueError("联系电话须为11位大陆手机号")
        for label, value in (
            ("省", self.province),
            ("市", self.city),
            ("区", self.district),
        ):
            if not value or len(value) > 32:
                raise ValueError(f"{label}级行政区不能为空且不超过32字")
        if not self.detail_address or len(self.detail_address) > 100:
            raise ValueError("详细地址不能为空且不超过100字")

    def masked_summary(self) -> str:
        if len(self.receiver_name) >= 2:
            name = self.receiver_name[0] + "*" * (len(self.receiver_name) - 1)
        else:
            name = "*"
        phone = self.contact_phone[:3] + "****" + self.contact_phone[-4:]
        detail = "*" * len(self.detail_address)
        return f"{name} {phone} {self.province}{self.city}{self.district}{detail}"

    def delivery_in_range(self) -> bool:
        return self.province in DELIVERY_SUPPORTED_PROVINCES


def address_denial_reasons(
    *,
    order_status: str,
    payment_status: str,
    has_active_refund: bool,
) -> list[str]:
    reasons: list[str] = []
    if order_status != "pending_shipment":
        reasons.append(DENIAL_ORDER_NOT_PENDING_SHIPMENT)
    if payment_status != "succeeded":
        reasons.append(DENIAL_PAYMENT_NOT_SUCCEEDED)
    if has_active_refund:
        reasons.append(DENIAL_ACTIVE_REFUND_EXISTS)
    return reasons
