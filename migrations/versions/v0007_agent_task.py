"""步骤15：可恢复异步任务及执行租约。"""

VERSION = "0007"


def statements() -> list[str]:
    return [
        """CREATE TABLE IF NOT EXISTS agent_task (
            tenant_id VARCHAR(64) NOT NULL, task_id VARCHAR(64) NOT NULL,
            customer_id VARCHAR(64) NOT NULL, actor_subject_id VARCHAR(64) NOT NULL,
            thread_id VARCHAR(64) NOT NULL, task_type VARCHAR(64) NOT NULL,
            input_masked JSON NOT NULL, input_digest CHAR(64) NOT NULL,
            status VARCHAR(32) NOT NULL, revision BIGINT UNSIGNED NOT NULL DEFAULT 0,
            checkpoint_ref VARCHAR(128) NULL, result_ref VARCHAR(128) NULL,
            error VARCHAR(512) NULL, idempotency_key VARCHAR(128) NOT NULL,
            lease_owner VARCHAR(64) NULL, lease_until DATETIME(6) NULL,
            fencing_token BIGINT UNSIGNED NOT NULL DEFAULT 0,
            deadline_at DATETIME(6) NOT NULL, created_at DATETIME(6) NOT NULL,
            updated_at DATETIME(6) NOT NULL,
            PRIMARY KEY (tenant_id,task_id),
            UNIQUE KEY uq_agent_task_request
                (tenant_id,actor_subject_id,task_type,idempotency_key),
            KEY idx_agent_task_claim (tenant_id,status,lease_until),
            KEY idx_agent_task_customer (tenant_id,customer_id,created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci"""
    ]
