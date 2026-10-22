"""Persist one insert-only manifest snapshot per tenant and knowledge release.

The migration runner replays MySQL DDL after interrupted applications, so the
single statement must be safe to execute more than once. Writers must insert
once and never update an existing snapshot.
"""

VERSION = "0011"


def statements() -> list[str]:
    return [
        "CREATE TABLE IF NOT EXISTS knowledge_manifest ("
        " tenant_id VARCHAR(64) NOT NULL, knowledge_release_id VARCHAR(64) NOT NULL,"
        " manifest_json LONGTEXT NOT NULL, manifest_digest CHAR(64) NOT NULL,"
        " created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),"
        " PRIMARY KEY (tenant_id, knowledge_release_id),"
        " CONSTRAINT fk_knowledge_manifest_release"
        " FOREIGN KEY (tenant_id, knowledge_release_id)"
        " REFERENCES knowledge_release (tenant_id, knowledge_release_id)"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
    ]
