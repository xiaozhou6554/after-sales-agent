"""步骤05配置测试：配置类型、必需项与环境模式校验，以及敏感值脱敏。"""

import pytest
from bootstrap.settings import RuntimeSettings
from pydantic import ValidationError


def make_settings(**overrides: object) -> RuntimeSettings:
    return RuntimeSettings(_env_file=None, **overrides)


def test_defaults_follow_global_param_table() -> None:
    settings = make_settings()
    assert settings.app_env == "development"
    assert settings.agent_max_steps == 8
    assert settings.tool_max_retries == 1
    assert settings.embedding_dimension == 512
    assert settings.embedding_model == "BAAI/bge-small-zh-v1.5"
    assert settings.vector_backend == "chroma"
    assert settings.memory_fallback_enabled is False
    assert settings.neo4j_read_only is True
    # 连接/读取超时（技术详细规格 1.4）
    assert settings.mysql_connect_timeout_s == 1.0
    assert settings.mysql_read_timeout_s == 2.0
    assert settings.redis_connect_timeout_s == 0.5
    assert settings.redis_read_timeout_s == 1.0
    assert settings.neo4j_connection_timeout_s == 1.0
    assert settings.mcp_read_timeout_s == 3.0
    assert settings.llm_read_timeout_s == 8.0


def test_invalid_app_env_rejected() -> None:
    with pytest.raises(ValidationError):
        make_settings(app_env="staging")


def test_invalid_vector_backend_rejected() -> None:
    with pytest.raises(ValidationError):
        make_settings(vector_backend="pgvector")


def test_embedding_dimension_is_hard_contract() -> None:
    with pytest.raises(ValidationError):
        make_settings(embedding_dimension=768)


def test_agent_step_and_retry_budgets_are_hard_constraints() -> None:
    with pytest.raises(ValidationError):
        make_settings(agent_max_steps=6)
    with pytest.raises(ValidationError):
        make_settings(tool_max_retries=2)


def test_production_requires_llm_api_key() -> None:
    with pytest.raises(ValidationError):
        make_settings(
            app_env="production",
            llm_api_key="",
            address_encrypt_key="test-prod-address-key",
        )
    settings = make_settings(
        app_env="production",
        llm_api_key="real-key",
        llm_base_url="https://llm.internal/v1",
        neo4j_query_password="test-only-secret-123",
        address_encrypt_key="test-prod-address-key",
    )
    assert settings.llm_api_key == "real-key"


def test_production_requires_distinct_neo4j_reader_credentials() -> None:
    base = {
        "app_env": "production",
        "llm_api_key": "real-key",
        "llm_base_url": "https://llm.internal/v1",
        "address_encrypt_key": "test-prod-address-key",
    }
    with pytest.raises(ValidationError):
        make_settings(**base)
    with pytest.raises(ValidationError):
        make_settings(**base, neo4j_query_username="neo4j", neo4j_query_password="reader-secret")
    with pytest.raises(ValidationError):
        make_settings(**base, neo4j_query_password="reader-secret", neo4j_read_only=False)


def test_production_forbids_memory_fallback() -> None:
    with pytest.raises(ValidationError):
        make_settings(
            app_env="production",
            llm_api_key="real-key",
            memory_fallback_enabled=True,
            address_encrypt_key="test-prod-address-key",
        )


def test_production_forbids_dev_cloud_llm_endpoint() -> None:
    with pytest.raises(ValidationError):
        make_settings(
            app_env="production",
            llm_api_key="real-key",
            llm_base_url="https://api.deepseek.com/v1",
            address_encrypt_key="test-prod-address-key",
        )


def test_production_requires_dedicated_address_encrypt_key() -> None:
    with pytest.raises(ValidationError):
        make_settings(app_env="production", llm_api_key="real-key")
    with pytest.raises(ValidationError):
        make_settings(
            app_env="production",
            llm_api_key="real-key",
            address_encrypt_key="dev-only-shipping-address-encrypt-key",
        )
    settings = make_settings(
        app_env="production",
        llm_api_key="real-key",
        llm_base_url="https://llm.internal/v1",
        neo4j_query_password="test-only-secret-123",
        address_encrypt_key="prod-address-key",
    )
    assert settings.address_encrypt_key == "prod-address-key"


def test_secrets_never_leak_through_repr_or_str() -> None:
    settings = make_settings(
        llm_api_key="super-secret-key",
        neo4j_password="neo4j-secret",
        mysql_url="mysql+pymysql://user:db-pass@localhost:3306/after_sales",
    )
    for text in (repr(settings), str(settings)):
        assert "super-secret-key" not in text
        assert "neo4j-secret" not in text
        assert "db-pass" not in text


def test_mask_text_and_safe_dict_mask_all_credentials() -> None:
    settings = make_settings(
        llm_api_key="super-secret-key",
        neo4j_password="neo4j-secret",
        mysql_url="mysql+pymysql://user:db-pass@localhost:3306/after_sales",
        rabbitmq_url="amqp://mq-user:mq-pass@localhost:5672/",
    )
    leaked = "key=super-secret-key db=db-pass mq=mq-pass graph=neo4j-secret"
    masked = settings.mask_text(leaked)
    assert "super-secret-key" not in masked
    assert "db-pass" not in masked
    assert "mq-pass" not in masked
    assert "neo4j-secret" not in masked

    safe = settings.to_safe_dict()
    assert safe["llm_api_key"] == "***"
    assert safe["neo4j_password"] == "***"
    assert "db-pass" not in safe["mysql_url"]
    assert "mq-pass" not in safe["rabbitmq_url"]
    assert "user:***@localhost" in safe["mysql_url"]
