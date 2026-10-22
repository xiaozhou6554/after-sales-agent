"""步骤14：Outbox、Inbox 与业务键去重的本地事务表。"""

VERSION = "0006"


def statements() -> list[str]:
    suffix = " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci"
    return [
        """CREATE TABLE IF NOT EXISTS outbox_event (
            tenant_id VARCHAR(64) NOT NULL, event_id VARCHAR(64) NOT NULL,
            owner_module VARCHAR(64) NOT NULL, event_type VARCHAR(128) NOT NULL,
            event_version INT UNSIGNED NOT NULL, schema_version VARCHAR(16) NOT NULL,
            request_id VARCHAR(64) NOT NULL, correlation_id VARCHAR(64) NOT NULL,
            trace_id VARCHAR(64) NOT NULL, thread_id VARCHAR(64) NULL,
            actor_subject_id VARCHAR(64) NOT NULL, customer_id VARCHAR(64) NULL,
            aggregate_type VARCHAR(64) NOT NULL, aggregate_id VARCHAR(64) NOT NULL,
            idempotency_key VARCHAR(128) NOT NULL, payload JSON NOT NULL,
            status VARCHAR(32) NOT NULL, attempts INT UNSIGNED NOT NULL DEFAULT 0,
            next_retry_at DATETIME(6) NOT NULL, lease_owner VARCHAR(64) NULL,
            lease_until DATETIME(6) NULL, published_at DATETIME(6) NULL,
            created_at DATETIME(6) NOT NULL,
            PRIMARY KEY (tenant_id,event_id),
            KEY idx_outbox_claim (owner_module,tenant_id,status,next_retry_at),
            KEY idx_outbox_lease (status,lease_until)
        )"""
        + suffix,
        """CREATE TABLE IF NOT EXISTS consumer_inbox (
            tenant_id VARCHAR(64) NOT NULL, inbox_id VARCHAR(64) NOT NULL,
            consumer_name VARCHAR(64) NOT NULL, event_id VARCHAR(64) NOT NULL,
            event_type VARCHAR(128) NOT NULL, idempotency_key VARCHAR(128) NOT NULL,
            request_digest CHAR(64) NOT NULL, status VARCHAR(32) NOT NULL,
            attempts INT UNSIGNED NOT NULL DEFAULT 0, lease_owner VARCHAR(64) NULL,
            lease_until DATETIME(6) NULL, fencing_token BIGINT UNSIGNED NOT NULL DEFAULT 0,
            processed_at DATETIME(6) NULL, result_ref VARCHAR(128) NULL,
            error VARCHAR(512) NULL,
            PRIMARY KEY (tenant_id,inbox_id),
            UNIQUE KEY uq_inbox_event (tenant_id,consumer_name,event_id),
            KEY idx_inbox_lease (tenant_id,consumer_name,status,lease_until)
        )"""
        + suffix,
        """CREATE TABLE IF NOT EXISTS consumer_business_guard (
            tenant_id VARCHAR(64) NOT NULL, guard_id VARCHAR(64) NOT NULL,
            consumer_name VARCHAR(64) NOT NULL, event_type VARCHAR(128) NOT NULL,
            idempotency_key VARCHAR(128) NOT NULL, request_digest CHAR(64) NOT NULL,
            status VARCHAR(32) NOT NULL, result_ref VARCHAR(128) NULL,
            revision BIGINT UNSIGNED NOT NULL DEFAULT 0,
            PRIMARY KEY (tenant_id,guard_id),
            UNIQUE KEY uq_guard_business
                (tenant_id,consumer_name,event_type,idempotency_key)
        )"""
        + suffix,
    ]
