"""Server-trusted, session-bound ticket tool intent sidecar."""

VERSION = "0017"


def statements() -> list[str]:
    return [
        "CREATE TABLE IF NOT EXISTS ticket_tool_intent ("
        " tenant_id VARCHAR(64) NOT NULL, confirmation_id VARCHAR(64) NOT NULL,"
        " actor_subject_id VARCHAR(64) NOT NULL, customer_id VARCHAR(64) NOT NULL,"
        " session_id VARCHAR(128) NOT NULL, operation_type VARCHAR(32) NOT NULL,"
        " ticket_id VARCHAR(64) NOT NULL, visible_digest CHAR(64) NOT NULL,"
        " visible_fields JSON NOT NULL, payload_masked JSON NOT NULL,"
        " payload_digest CHAR(64) NOT NULL, idempotency_key VARCHAR(64) NOT NULL,"
        " expected_revision BIGINT UNSIGNED NULL, expires_at DATETIME(6) NOT NULL,"
        " created_at DATETIME(6) NOT NULL,"
        " PRIMARY KEY (tenant_id, confirmation_id),"
        " KEY ix_ticket_tool_intent_match"
        " (tenant_id, actor_subject_id, session_id, operation_type, visible_digest),"
        " CONSTRAINT fk_ticket_tool_intent_confirmation"
        " FOREIGN KEY (tenant_id, confirmation_id)"
        " REFERENCES operation_confirmation (tenant_id, confirmation_id)"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
    ]
