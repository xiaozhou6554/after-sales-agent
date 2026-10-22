"""进程运行配置：统一读取、类型校验、必填密钥检查与敏感值脱敏。

数值默认值全部来自《技术详细规格》1.4 全局参数表（唯一参数源），不另设一套默认；
生产环境的必填项与禁止项在此强制校验。本模块任何输出（repr/str/日志辅助）都不
泄露密钥与连接凭据。
"""

from __future__ import annotations

import sys
from urllib.parse import urlparse, urlunparse

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ALLOWED_APP_ENV = ("development", "test", "production")
ALLOWED_VECTOR_BACKEND = ("chroma", "milvus")

# 开发经批准使用的云 API 端点；生产必须使用私有模型端点（技术详细规格 1.3）。
DEV_CLOUD_LLM_HOST = "api.deepseek.com"


def _mask_url_password(url: str) -> str:
    """将 URL 中的密码部分替换为 ***，其余部分保持原样。"""
    parsed = urlparse(url)
    if not parsed.password:
        return url
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    netloc = host
    if parsed.username:
        netloc = f"{parsed.username}:***@{host}"
    return urlunparse(parsed._replace(netloc=netloc))


class RuntimeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"

    api_host: str = "127.0.0.1"
    api_port: int = 8000

    llm_provider: str = "deepseek"
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_model: str = "deepseek-chat"
    llm_api_key: str = ""
    # WooCommerce is an opt-in read source for order/payment queries in the MCP runtime.
    woo_order_queries_enabled: bool = False
    woo_store_url: str = ""
    woo_consumer_key: str = ""
    woo_consumer_secret: str = ""
    woo_test_order_id: str = ""
    woo_test_tenant_id: str = ""
    woo_test_refund_amount: str = ""
    woo_enable_refund_write: bool = False
    llm_tool_choice: str = "auto"
    llm_temperature: float = 0.2

    agent_max_steps: int = 8
    tool_max_retries: int = 1
    ticket_confirm_ttl_seconds: int = 300
    refund_confirm_ttl_seconds: int = 300
    address_confirm_ttl_seconds: int = 300
    # 收货地址为受限个人信息：落库仅存密文；密钥经环境注入。
    address_encrypt_key: str = "dev-only-shipping-address-encrypt-key"

    mcp_server_url: str = "http://127.0.0.1:8001/mcp"
    mcp_server_host: str = "127.0.0.1"
    mcp_server_port: int = 8001

    mysql_url: str = "mysql+pymysql://after_sales:change-me@localhost:3306/after_sales"

    redis_url: str = "redis://localhost:6379/0"
    memory_fallback_enabled: bool = False

    rabbitmq_url: str = "amqp://after_sales:change-me@localhost:5672/"

    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_username: str = "neo4j"
    neo4j_password: str = "change-me"
    neo4j_query_username: str = "knowledge_reader"
    neo4j_query_password: str = ""
    knowledge_local_tools_enabled: bool = False
    # 注意：Neo4j 5 数据库名仅允许字母/数字/点/横线，不允许下划线。
    # 规格 13 章写的 after_sales_knowledge 在平台上无法成立，故用连字符命名；
    # 已在开发运行指南中标记为规格修订事项。
    neo4j_database: str = "after-sales-knowledge"
    neo4j_read_only: bool = True

    vector_backend: str = "chroma"
    chroma_persist_dir: str = "./data/generated/chroma"
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_model_path: str = "./data/generated/models/bge-small-zh-v1.5"
    embedding_model_revision: str = "7999e1d3359715c523056ef9478215996d62a620"
    embedding_dimension: int = 512
    vector_top_k: int = 20
    bm25_top_k: int = 20
    hnsw_m: int = 16
    hnsw_ef_construction: int = 200
    hnsw_ef_search: int = 64

    # 以下连接/读取超时均来自《技术详细规格》1.4 全局参数表。
    mysql_connect_timeout_s: float = 1.0
    mysql_read_timeout_s: float = 2.0
    redis_connect_timeout_s: float = 0.5
    redis_read_timeout_s: float = 1.0
    neo4j_connection_timeout_s: float = 1.0
    neo4j_read_timeout_s: float = 2.0
    rabbitmq_connect_timeout_s: float = 1.0
    rabbitmq_read_timeout_s: float = 2.0
    llm_connect_timeout_s: float = 1.0
    llm_read_timeout_s: float = 8.0
    mcp_connect_timeout_s: float = 1.0
    mcp_read_timeout_s: float = 3.0
    retrieval_connect_timeout_s: float = 0.5
    retrieval_read_timeout_s: float = 2.0

    @model_validator(mode="after")
    def _validate_environment_rules(self) -> "RuntimeSettings":
        if self.app_env not in ALLOWED_APP_ENV:
            raise ValueError(f"APP_ENV 必须是 {'/'.join(ALLOWED_APP_ENV)}，当前为 {self.app_env!r}")
        if self.vector_backend not in ALLOWED_VECTOR_BACKEND:
            allowed = "/".join(ALLOWED_VECTOR_BACKEND)
            raise ValueError(f"VECTOR_BACKEND 必须是 {allowed}，当前为 {self.vector_backend!r}")
        # Embedding 维度是硬契约：模型、索引和查询向量必须一致。
        if self.embedding_dimension != 512:
            raise ValueError("EMBEDDING_DIMENSION 必须为 512（技术详细规格 1.4 硬契约）")
        if min(self.vector_top_k, self.bm25_top_k, self.hnsw_m, self.hnsw_ef_construction) <= 0:
            raise ValueError("检索召回数量与HNSW建索引参数必须大于零")
        if self.hnsw_ef_search < self.vector_top_k:
            raise ValueError("HNSW_EF_SEARCH 必须不小于向量召回数量")
        # Agent 步数与只读重试预算为硬约束，不在环境层面放开。
        if self.agent_max_steps != 8:
            raise ValueError("AGENT_MAX_STEPS 硬约束为 8（技术详细规格 1.4），不得经配置修改")
        if self.tool_max_retries != 1:
            raise ValueError("TOOL_MAX_RETRIES 硬约束为 1（技术详细规格 1.4），不得经配置修改")
        if self.ticket_confirm_ttl_seconds <= 0 or self.refund_confirm_ttl_seconds <= 0:
            raise ValueError("确认有效期必须大于零")
        if self.address_confirm_ttl_seconds <= 0:
            raise ValueError("确认有效期必须大于零")
        if self.woo_order_queries_enabled:
            if self.app_env == "production":
                raise ValueError("WOO_ORDER_QUERIES_ENABLED 仅允许在开发/测试环境启用")
            if not all((self.woo_store_url, self.woo_consumer_key, self.woo_consumer_secret)):
                raise ValueError(
                    "启用 WooCommerce 订单查询时必须配置 WOO_STORE_URL、"
                    "WOO_CONSUMER_KEY 和 WOO_CONSUMER_SECRET"
                )
        if self.app_env == "production" and (
            not self.address_encrypt_key
            or self.address_encrypt_key == "dev-only-shipping-address-encrypt-key"
        ):
            raise ValueError("生产环境必须配置独立的收货地址加密密钥")
        if self.app_env == "production":
            if (
                self.neo4j_query_username == self.neo4j_username
                or not self.neo4j_query_password
                or not self.neo4j_read_only
            ):
                raise ValueError("生产知识图谱必须使用独立只读账号与非默认密码")
            if not self.llm_api_key:
                raise ValueError("生产环境必须配置 LLM_API_KEY")
            if self.memory_fallback_enabled:
                raise ValueError("生产环境禁止 MEMORY_FALLBACK_ENABLED=true（技术详细规格 15）")
            if DEV_CLOUD_LLM_HOST in self.llm_base_url:
                raise ValueError("生产环境必须使用私有模型端点，不得使用开发云 API")
        return self

    def secret_strings(self) -> list[str]:
        """收集需要在日志与错误信息中掩码的原始密钥片段。"""
        secrets = [
            self.neo4j_password,
            self.neo4j_query_password,
            self.llm_api_key,
            self.woo_consumer_key,
            self.woo_consumer_secret,
            self.address_encrypt_key,
        ]
        for url in (self.mysql_url, self.redis_url, self.rabbitmq_url):
            password = urlparse(url).password
            if password:
                secrets.append(password)
        return [item for item in secrets if item]

    def mask_text(self, text: str) -> str:
        """替换文本中出现的任何已知密钥片段，用于日志与探测错误输出。"""
        masked = text
        for secret in self.secret_strings():
            masked = masked.replace(secret, "***")
        return masked

    def to_safe_dict(self) -> dict[str, str]:
        """输出可安全打印的配置摘要，密钥与 URL 凭据一律掩码。"""
        return {
            "app_env": self.app_env,
            "llm_base_url": self.llm_base_url,
            "llm_model": self.llm_model,
            "llm_api_key": "***" if self.llm_api_key else "",
            "woo_store_url": self.woo_store_url,
            "woo_order_queries_enabled": str(self.woo_order_queries_enabled).lower(),
            "woo_consumer_key": "***" if self.woo_consumer_key else "",
            "woo_consumer_secret": "***" if self.woo_consumer_secret else "",
            "mysql_url": _mask_url_password(self.mysql_url),
            "redis_url": _mask_url_password(self.redis_url),
            "rabbitmq_url": _mask_url_password(self.rabbitmq_url),
            "neo4j_uri": self.neo4j_uri,
            "neo4j_username": self.neo4j_username,
            "neo4j_password": "***",
            "neo4j_query_username": self.neo4j_query_username,
            "neo4j_query_password": "***",
            "vector_backend": self.vector_backend,
            "memory_fallback_enabled": str(self.memory_fallback_enabled),
            "address_encrypt_key": "***" if self.address_encrypt_key else "",
        }

    def __repr__(self) -> str:
        return (
            "RuntimeSettings(app_env={0.app_env!r}, llm_model={0.llm_model!r}, "
            "vector_backend={0.vector_backend!r}, secrets=***)".format(self)
        )

    __str__ = __repr__


def load_settings() -> RuntimeSettings:
    settings = RuntimeSettings()
    # 控制台为 GBK 时避免中文输出崩溃；不影响文件与日志编码。
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    return settings
