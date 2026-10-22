"""Bounded Mock human handoff queue and request idempotency."""

VERSION = "0016"
_SUFFIX = " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"


def statements() -> list[str]:
    return [
        "CREATE TABLE IF NOT EXISTS human_handoff_claim ("
        " tenant_id VARCHAR(64) NOT NULL, handoff_id VARCHAR(64) NOT NULL,"
        " actor_id VARCHAR(64) NOT NULL, idempotency_key VARCHAR(64) NOT NULL,"
        " PRIMARY KEY (tenant_id, handoff_id),"
        " CONSTRAINT fk_handoff_claim_handoff FOREIGN KEY (tenant_id, handoff_id)"
        " REFERENCES human_handoff (tenant_id, handoff_id))" + _SUFFIX,
        "CREATE TABLE IF NOT EXISTS human_handoff_capacity ("
        " tenant_id VARCHAR(64) NOT NULL, active_count INT UNSIGNED NOT NULL DEFAULT 0,"
        " PRIMARY KEY (tenant_id))" + _SUFFIX,
        "CREATE TABLE IF NOT EXISTS human_handoff_idempotency ("
        " tenant_id VARCHAR(64) NOT NULL, requested_by VARCHAR(64) NOT NULL,"
        " idempotency_key VARCHAR(64) NOT NULL, request_digest CHAR(64) NOT NULL,"
        " handoff_id VARCHAR(64) NOT NULL, created_at DATETIME(6) NOT NULL,"
        " PRIMARY KEY (tenant_id, requested_by, idempotency_key),"
        " CONSTRAINT fk_handoff_idem_handoff FOREIGN KEY (tenant_id, handoff_id)"
        " REFERENCES human_handoff (tenant_id, handoff_id))" + _SUFFIX,
    ]
