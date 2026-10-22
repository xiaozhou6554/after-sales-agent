"""步骤28：通知记录与发送状态（Mock 渠道；unknown 表示结果待核实）。"""

VERSION = "0024"
_SUFFIX = " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"


def statements() -> list[str]:
    return [
        "CREATE TABLE IF NOT EXISTS notification ("
        " tenant_id VARCHAR(64) NOT NULL, notification_id VARCHAR(64) NOT NULL,"
        " customer_id VARCHAR(64) NULL, channel VARCHAR(32) NOT NULL,"
        " template_id VARCHAR(64) NOT NULL, resource_refs JSON NOT NULL,"
        " actor_subject_id VARCHAR(64) NOT NULL,"
        " status VARCHAR(16) NOT NULL,"
        " error VARCHAR(512) NULL,"
        " sent_at DATETIME(6) NULL,"
        " created_at DATETIME(6) NOT NULL, updated_at DATETIME(6) NOT NULL,"
        " PRIMARY KEY (tenant_id, notification_id),"
        " KEY ix_notification_customer (tenant_id, customer_id, created_at),"
        " KEY ix_notification_status (tenant_id, status),"
        " CONSTRAINT chk_notification_status CHECK ("
        " status IN ('pending','sent','failed','unknown'))"
        ")" + _SUFFIX + " COMMENT='步骤28:通知发送状态(mock渠道,unknown=待核实)'",
    ]
