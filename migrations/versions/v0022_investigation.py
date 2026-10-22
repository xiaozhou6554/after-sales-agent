"""步骤25D：异常调查与修订（不可变修订历史，固定快照可重放）。"""

VERSION = "0022"
_SUFFIX = " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"


def statements() -> list[str]:
    return [
        "CREATE TABLE IF NOT EXISTS investigation ("
        " tenant_id VARCHAR(64) NOT NULL, investigation_id VARCHAR(64) NOT NULL,"
        " anomaly_type VARCHAR(32) NOT NULL,"
        " subject VARCHAR(255) NOT NULL,"
        " status VARCHAR(16) NOT NULL,"
        " current_revision INT UNSIGNED NOT NULL,"
        " environment VARCHAR(32) NOT NULL,"
        " created_by VARCHAR(64) NOT NULL,"
        " created_at DATETIME(6) NOT NULL, updated_at DATETIME(6) NOT NULL,"
        " PRIMARY KEY (tenant_id, investigation_id),"
        " KEY ix_investigation_subject (tenant_id, anomaly_type, subject),"
        " CONSTRAINT chk_investigation_type CHECK (anomaly_type IN"
        " ('business_metric','cross_system_conflict','runtime_failure')),"
        " CONSTRAINT chk_investigation_status CHECK (status IN ('open','closed')),"
        " CONSTRAINT chk_investigation_revision CHECK (current_revision >= 1)"
        ")" + _SUFFIX,
        "CREATE TABLE IF NOT EXISTS investigation_revision ("
        " tenant_id VARCHAR(64) NOT NULL, investigation_id VARCHAR(64) NOT NULL,"
        " revision INT UNSIGNED NOT NULL,"
        " basis_snapshot_version VARCHAR(32) NOT NULL,"
        " basis_as_of DATE NOT NULL,"
        " cause_status VARCHAR(32) NOT NULL,"
        " gate_json JSON NOT NULL, findings_json JSON NOT NULL,"
        " metric_version VARCHAR(32) NULL, ontology_version VARCHAR(32) NULL,"
        " mapping_version VARCHAR(32) NULL, snapshot_id VARCHAR(64) NOT NULL,"
        " reason VARCHAR(255) NULL,"
        " created_by VARCHAR(64) NOT NULL, created_at DATETIME(6) NOT NULL,"
        " PRIMARY KEY (tenant_id, investigation_id, revision),"
        " CONSTRAINT chk_investigation_revision_no CHECK (revision >= 1),"
        " CONSTRAINT chk_investigation_cause CHECK (cause_status IN"
        " ('supported','inconclusive','data_quality_blocked'))"
        ")" + _SUFFIX,
    ]
