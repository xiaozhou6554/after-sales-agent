"""Explicit opt-in wiring for local WooCommerce order reads."""

import pytest
from bootstrap.container import build_order_query_adapter
from bootstrap.settings import RuntimeSettings

from infrastructure.mock.order_adapter import MockOrderAdapter
from infrastructure.order.woocommerce_adapter import WooCommerceOrderAdapter


def test_mock_order_adapter_remains_the_default() -> None:
    settings = RuntimeSettings(_env_file=None)

    adapter = build_order_query_adapter(settings, engine=object())

    assert isinstance(adapter, MockOrderAdapter)


def test_enabled_woocommerce_selects_read_adapter() -> None:
    settings = RuntimeSettings(
        _env_file=None,
        woo_order_queries_enabled=True,
        woo_store_url="http://localhost:8088",
        woo_consumer_key="ck_test",
        woo_consumer_secret="cs_test",
    )

    adapter = build_order_query_adapter(settings, engine=object())

    assert isinstance(adapter, WooCommerceOrderAdapter)
    adapter.close()


def test_woocommerce_read_adapter_requires_explicit_rest_configuration() -> None:
    with pytest.raises(ValueError, match="必须配置 WOO_STORE_URL"):
        RuntimeSettings(_env_file=None, woo_order_queries_enabled=True)


def test_woocommerce_order_reads_are_forbidden_in_production() -> None:
    with pytest.raises(ValueError, match="仅允许在开发/测试环境启用"):
        RuntimeSettings(
            _env_file=None,
            app_env="production",
            woo_order_queries_enabled=True,
            woo_store_url="https://store.example",
            woo_consumer_key="ck_test",
            woo_consumer_secret="cs_test",
            llm_api_key="test-llm-key",
            llm_base_url="https://llm.internal/v1",
            neo4j_query_username="neo4j_reader",
            neo4j_query_password="reader-secret",
            neo4j_read_only=True,
            address_encrypt_key="test-prod-address-key",
        )
