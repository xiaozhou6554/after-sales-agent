"""步骤16：知识发布候选、文档及后续三路索引任务的持久化表。"""

VERSION = "0009"
_SUFFIX = " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"


def statements() -> list[str]:
    return [
        "CREATE TABLE IF NOT EXISTS knowledge_release ("
        " tenant_id VARCHAR(64) NOT NULL, knowledge_release_id VARCHAR(64) NOT NULL,"
        " version VARCHAR(64) NOT NULL, manifest_digest CHAR(64) NOT NULL,"
        " status VARCHAR(32) NOT NULL, revision BIGINT UNSIGNED NOT NULL DEFAULT 0,"
        " is_active TINYINT(1) NOT NULL DEFAULT 0, published_at DATETIME(6) NULL,"
        " failure_reason VARCHAR(255) NULL, PRIMARY KEY (tenant_id, knowledge_release_id),"
        " UNIQUE KEY uq_knowledge_version (tenant_id, version)"
        ")" + _SUFFIX,
        "CREATE TABLE IF NOT EXISTS knowledge_document ("
        " tenant_id VARCHAR(64) NOT NULL, knowledge_release_id VARCHAR(64) NOT NULL,"
        " document_id VARCHAR(64) NOT NULL, title VARCHAR(255) NOT NULL,"
        " source_ref VARCHAR(255) NOT NULL, content_digest CHAR(64) NOT NULL,"
        " version VARCHAR(64) NOT NULL, effective_from DATE NULL, effective_to DATE NULL,"
        " change_type VARCHAR(32) NOT NULL,"
        " PRIMARY KEY (tenant_id, knowledge_release_id, document_id),"
        " CONSTRAINT fk_knowledge_document_release"
        " FOREIGN KEY (tenant_id, knowledge_release_id)"
        " REFERENCES knowledge_release (tenant_id, knowledge_release_id)"
        ")" + _SUFFIX,
        "CREATE TABLE IF NOT EXISTS knowledge_index_job ("
        " tenant_id VARCHAR(64) NOT NULL, job_id VARCHAR(64) NOT NULL,"
        " knowledge_release_id VARCHAR(64) NOT NULL, backend VARCHAR(16) NOT NULL,"
        " manifest_digest CHAR(64) NOT NULL, status VARCHAR(32) NOT NULL,"
        " revision BIGINT UNSIGNED NOT NULL DEFAULT 0, attempts INT NOT NULL DEFAULT 0,"
        " lease_owner VARCHAR(64) NULL, lease_until DATETIME(6) NULL,"
        " error VARCHAR(255) NULL, completed_at DATETIME(6) NULL,"
        " PRIMARY KEY (tenant_id, job_id),"
        " UNIQUE KEY uq_knowledge_index_backend (tenant_id, knowledge_release_id, backend),"
        " CONSTRAINT fk_knowledge_job_release FOREIGN KEY (tenant_id, knowledge_release_id)"
        " REFERENCES knowledge_release (tenant_id, knowledge_release_id)"
        ")" + _SUFFIX,
    ]
