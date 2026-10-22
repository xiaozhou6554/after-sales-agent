"""审计基础表：agent_audit_log（技术详细规格 11.1、25 章审计字段全集）。

- 追加不可变：事前意图 status='pending'，事后在同一行追加结果；
- 主键 tenant_id+audit_id；存在 event_id 时 tenant_id+event_id 唯一；
- 索引 tenant+trace（一次请求串联 API/Agent/MCP）与 tenant+actor+created_at；
- parameter_summary/result_summary 只允许存脱敏后摘要（16.2/543 行）。
"""

VERSION = "0003"

_TABLE_SUFFIX = " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci"


def statements() -> list[str]:
    return [
        """
        CREATE TABLE IF NOT EXISTS agent_audit_log (
            audit_id VARCHAR(64) NOT NULL,
            tenant_id VARCHAR(64) NOT NULL,
            event_id VARCHAR(64) NULL,
            request_id VARCHAR(64) NOT NULL,
            correlation_id VARCHAR(64) NOT NULL,
            trace_id VARCHAR(64) NOT NULL,
            thread_id VARCHAR(64) NULL,
            actor_subject_id VARCHAR(64) NOT NULL,
            customer_id VARCHAR(64) NULL,
            action VARCHAR(128) NOT NULL,
            resource_refs JSON NOT NULL,
            parameter_summary JSON NOT NULL,
            result_summary JSON NULL,
            duration_ms INT NULL,
            status VARCHAR(32) NOT NULL,
            error_code VARCHAR(64) NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (tenant_id, audit_id),
            UNIQUE KEY uq_audit_tenant_event (tenant_id, event_id),
            KEY idx_audit_tenant_trace (tenant_id, trace_id),
            KEY idx_audit_tenant_actor_time (tenant_id, actor_subject_id, created_at)
        ){suffix}
        """.format(suffix=_TABLE_SUFFIX),
    ]
