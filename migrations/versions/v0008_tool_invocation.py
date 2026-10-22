"""步骤15：工具调用意图、统一尝试预算与未知结果持久化。"""

VERSION = "0008"


def statements() -> list[str]:
    return [
        """CREATE TABLE IF NOT EXISTS tool_invocation (
            tenant_id VARCHAR(64) NOT NULL, invocation_id VARCHAR(64) NOT NULL,
            request_id VARCHAR(64) NOT NULL, agent_id VARCHAR(64) NOT NULL,
            tool_name VARCHAR(128) NOT NULL, normalized_args_digest CHAR(64) NOT NULL,
            args_summary JSON NOT NULL, access_mode VARCHAR(16) NOT NULL,
            attempt_count INT UNSIGNED NOT NULL DEFAULT 0,
            retry_count INT UNSIGNED NOT NULL DEFAULT 0,
            status VARCHAR(32) NOT NULL, result_summary JSON NULL,
            error VARCHAR(128) NULL, revision BIGINT UNSIGNED NOT NULL DEFAULT 0,
            lease_owner VARCHAR(64) NULL, lease_until DATETIME(6) NULL,
            fencing_token BIGINT UNSIGNED NOT NULL DEFAULT 0,
            created_at DATETIME(6) NOT NULL, updated_at DATETIME(6) NOT NULL,
            PRIMARY KEY (tenant_id,invocation_id),
            UNIQUE KEY uq_tool_invocation_request
                (tenant_id,request_id,tool_name,normalized_args_digest),
            KEY idx_tool_invocation_status (tenant_id,status,lease_until)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci"""
    ]
