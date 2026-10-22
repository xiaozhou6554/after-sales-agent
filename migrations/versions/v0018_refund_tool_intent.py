"""Bind development refund MCP writes to a server-issued session and confirmation."""

VERSION = "0018"
_SUFFIX = " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"


def statements() -> list[str]:
    return [
        "CREATE TABLE IF NOT EXISTS refund_tool_intent ("
        " tenant_id VARCHAR(64) NOT NULL, confirmation_id VARCHAR(64) NOT NULL,"
        " actor_subject_id VARCHAR(64) NOT NULL, session_id VARCHAR(128) NOT NULL,"
        " quote_id VARCHAR(64) NOT NULL, order_id VARCHAR(64) NOT NULL,"
        " reason_code VARCHAR(64) NOT NULL, created_at DATETIME(6) NOT NULL,"
        " PRIMARY KEY (tenant_id, confirmation_id),"
        " KEY ix_refund_tool_intent_session"
        " (tenant_id, actor_subject_id, session_id, order_id, reason_code),"
        " CONSTRAINT fk_refund_tool_intent_confirmation FOREIGN KEY"
        " (tenant_id, confirmation_id) REFERENCES operation_confirmation"
        " (tenant_id, confirmation_id),"
        " CONSTRAINT fk_refund_tool_intent_quote FOREIGN KEY"
        " (tenant_id, quote_id) REFERENCES refund_quote (tenant_id, quote_id))" + _SUFFIX,
    ]
