"""步骤13：按租户和客户隔离的长期画像及事实。"""

VERSION = "0005"


def statements() -> list[str]:
    suffix = " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci"
    return [
        """CREATE TABLE IF NOT EXISTS user_profile (
            tenant_id VARCHAR(64) NOT NULL,
            user_id VARCHAR(64) NOT NULL,
            display_name VARCHAR(128) NULL,
            contact_masked VARCHAR(64) NULL,
            role VARCHAR(32) NOT NULL DEFAULT 'customer',
            created_at DATETIME(6) NOT NULL,
            updated_at DATETIME(6) NOT NULL,
            PRIMARY KEY (tenant_id, user_id)
        )"""
        + suffix,
        """CREATE TABLE IF NOT EXISTS user_memory_fact (
            tenant_id VARCHAR(64) NOT NULL,
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
            user_id VARCHAR(64) NOT NULL,
            fact_type VARCHAR(64) NOT NULL,
            fact_key VARCHAR(128) NOT NULL,
            fact_value JSON NOT NULL,
            importance DECIMAL(3,2) NOT NULL,
            source VARCHAR(32) NOT NULL,
            created_at DATETIME(6) NOT NULL,
            updated_at DATETIME(6) NOT NULL,
            PRIMARY KEY (tenant_id, id),
            KEY idx_memory_id (id),
            UNIQUE KEY uq_memory_fact (tenant_id, user_id, fact_type, fact_key),
            KEY idx_memory_user (tenant_id, user_id, updated_at),
            CONSTRAINT fk_memory_profile FOREIGN KEY (tenant_id, user_id)
                REFERENCES user_profile (tenant_id, user_id)
        )"""
        + suffix,
    ]
