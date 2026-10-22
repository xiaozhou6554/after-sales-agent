"""Agent Checkpoint 表：agent_checkpoint（技术详细规格 11.1，538 行字段全集）。

- 联合恢复键 tenant_id+thread_id+checkpoint_ns+checkpoint_id，禁止仅凭 thread 访问；
- revision 为服务端维护版本号（根 0，子=父+1），用于版本与并发校验；
- state/metadata/pending_writes 仅存脱敏后 JSON（16.2/543 行）；
- 索引 tenant+thread+ns+saved_at 供 TTL 清理与按时间检索。
"""

VERSION = "0004"

_TABLE_SUFFIX = " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci"


def statements() -> list[str]:
    return [
        """
        CREATE TABLE IF NOT EXISTS agent_checkpoint (
            tenant_id VARCHAR(64) NOT NULL,
            thread_id VARCHAR(64) NOT NULL,
            checkpoint_ns VARCHAR(128) NOT NULL DEFAULT '',
            checkpoint_id VARCHAR(64) NOT NULL,
            parent_checkpoint_id VARCHAR(64) NULL,
            current_node VARCHAR(64) NULL,
            state_schema_version VARCHAR(32) NOT NULL,
            revision BIGINT UNSIGNED NOT NULL,
            state JSON NOT NULL,
            metadata JSON NOT NULL,
            pending_writes JSON NOT NULL,
            saved_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (tenant_id, thread_id, checkpoint_ns, checkpoint_id),
            KEY idx_checkpoint_tenant_thread_ns_saved (
                tenant_id, thread_id, checkpoint_ns, saved_at
            ),
            KEY idx_checkpoint_tenant_thread_parent (
                tenant_id, thread_id, checkpoint_ns, parent_checkpoint_id
            )
        ){suffix}
        """.format(suffix=_TABLE_SUFFIX),
    ]
