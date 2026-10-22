"""步骤24：绑定确认及通用操作幂等记录；业务更新仍由领域模块拥有。"""

VERSION = "0010"
_SUFFIX = " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"


def statements() -> list[str]:
    return [
        "CREATE TABLE IF NOT EXISTS operation_confirmation ("
        " tenant_id VARCHAR(64) NOT NULL, confirmation_id VARCHAR(64) NOT NULL,"
        " actor_subject_id VARCHAR(64) NOT NULL, customer_id VARCHAR(64) NOT NULL,"
        " confirmer_subject_id VARCHAR(64) NOT NULL, operation_type VARCHAR(32) NOT NULL,"
        " target_id VARCHAR(64) NOT NULL, payload_digest CHAR(64) NOT NULL,"
        " expected_revision BIGINT UNSIGNED NULL, challenge_digest CHAR(64) NOT NULL,"
        " status VARCHAR(16) NOT NULL, expires_at DATETIME(6) NOT NULL,"
        " consumed_at DATETIME(6) NULL, consumed_by_operation_id VARCHAR(64) NULL,"
        " created_at DATETIME(6) NOT NULL, PRIMARY KEY (tenant_id, confirmation_id),"
        " KEY ix_confirmation_actor (tenant_id, actor_subject_id, operation_type, target_id)"
        ")" + _SUFFIX,
        "CREATE TABLE IF NOT EXISTS operation_idempotency ("
        " tenant_id VARCHAR(64) NOT NULL, record_id VARCHAR(64) NOT NULL,"
        " actor_subject_id VARCHAR(64) NOT NULL, operation_type VARCHAR(32) NOT NULL,"
        " idempotency_key VARCHAR(64) NOT NULL, request_digest CHAR(64) NOT NULL,"
        " resource_id VARCHAR(64) NULL, status VARCHAR(16) NOT NULL,"
        " result_ref VARCHAR(128) NULL, error VARCHAR(64) NULL,"
        " lease_until DATETIME(6) NULL, revision BIGINT UNSIGNED NOT NULL DEFAULT 0,"
        " created_at DATETIME(6) NOT NULL, updated_at DATETIME(6) NOT NULL,"
        " PRIMARY KEY (tenant_id, record_id),"
        " UNIQUE KEY uq_operation_idem"
        " (tenant_id, actor_subject_id, operation_type, idempotency_key)"
        ")" + _SUFFIX,
    ]
