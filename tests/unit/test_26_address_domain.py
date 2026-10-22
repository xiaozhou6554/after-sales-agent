"""步骤26 纯域单测：收货地址 VO 校验、脱敏与资格判定（TC-57/58 前置）。"""

import pytest

from application.order.use_cases.address_change import AddressChangeDenied
from domain.order.address import (
    DELIVERY_SUPPORTED_PROVINCES,
    DENIAL_ACTIVE_REFUND_EXISTS,
    DENIAL_DELIVERY_OUT_OF_RANGE,
    DENIAL_ORDER_NOT_PENDING_SHIPMENT,
    DENIAL_PAYMENT_NOT_SUCCEEDED,
    ShippingAddress,
    address_denial_reasons,
)


def _address(**overrides) -> ShippingAddress:
    fields = {
        "receiver_name": "张三",
        "contact_phone": "13812345678",
        "province": "上海市",
        "city": "上海市",
        "district": "徐汇区",
        "detail_address": "漕溪北路100号",
    }
    fields.update(overrides)
    return ShippingAddress(**fields)


class TestShippingAddressValidation:
    def test_valid_address_passes(self):
        _address().validate()

    @pytest.mark.parametrize("phone", ["12345678901", "1381234567", "23812345678", ""])
    def test_phone_must_be_mainland_mobile(self, phone):
        with pytest.raises(ValueError):
            _address(contact_phone=phone).validate()

    def test_receiver_name_length(self):
        with pytest.raises(ValueError):
            _address(receiver_name="张").validate()
        with pytest.raises(ValueError):
            _address(receiver_name="a" * 21).validate()

    def test_detail_address_required(self):
        with pytest.raises(ValueError):
            _address(detail_address="").validate()
        with pytest.raises(ValueError):
            _address(detail_address="街" * 101).validate()

    @pytest.mark.parametrize("field", ["province", "city", "district"])
    def test_region_fields_required(self, field):
        with pytest.raises(ValueError):
            _address(**{field: ""}).validate()


class TestMaskedSummary:
    def test_no_raw_phone_or_detail_leaks(self):
        masked = _address().masked_summary()
        assert "13812345678" not in masked
        assert "漕溪北路100号" not in masked
        assert "张三" not in masked
        assert "138****5678" in masked

    def test_single_char_name_fully_masked(self):
        masked = _address(receiver_name="明").masked_summary()
        assert masked.split(" ")[0] == "*"


class TestDeliveryRange:
    def test_supported_province_in_range(self):
        assert _address(province="广东省").delivery_in_range()

    def test_unsupported_province_out_of_range(self):
        assert not _address(province="青海省").delivery_in_range()

    def test_allowlist_is_mock_scope(self):
        assert DENIAL_DELIVERY_OUT_OF_RANGE
        assert isinstance(DELIVERY_SUPPORTED_PROVINCES, frozenset)


class TestDenialReasons:
    def test_eligible_order_has_no_reasons(self):
        reasons = address_denial_reasons(
            order_status="pending_shipment",
            payment_status="succeeded",
            has_active_refund=False,
        )
        assert reasons == []

    def test_each_blocker_reports_code(self):
        assert address_denial_reasons(
            order_status="delivered", payment_status="succeeded", has_active_refund=False
        ) == [DENIAL_ORDER_NOT_PENDING_SHIPMENT]
        assert address_denial_reasons(
            order_status="pending_shipment", payment_status="failed", has_active_refund=False
        ) == [DENIAL_PAYMENT_NOT_SUCCEEDED]
        assert address_denial_reasons(
            order_status="pending_shipment", payment_status="succeeded", has_active_refund=True
        ) == [DENIAL_ACTIVE_REFUND_EXISTS]
        all_blocked = address_denial_reasons(
            order_status="cancelled", payment_status="unpaid", has_active_refund=True
        )
        assert all_blocked == [
            DENIAL_ORDER_NOT_PENDING_SHIPMENT,
            DENIAL_PAYMENT_NOT_SUCCEEDED,
            DENIAL_ACTIVE_REFUND_EXISTS,
        ]

    def test_denied_error_carries_reasons(self):
        denied = AddressChangeDenied([DENIAL_ORDER_NOT_PENDING_SHIPMENT])
        assert denied.reasons == [DENIAL_ORDER_NOT_PENDING_SHIPMENT]
