"""步骤25：退款模块拥有的订单占用守卫，禁止换键并发重复预留。"""

VERSION = "0015"
_SUFFIX = " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"


def statements() -> list[str]:
    return [
        "CREATE TABLE IF NOT EXISTS refund_order_guard ("
        " tenant_id VARCHAR(64) NOT NULL, order_id VARCHAR(64) NOT NULL,"
        " active_request_id VARCHAR(64) NULL,"
        " reserved_goods_minor BIGINT UNSIGNED NOT NULL DEFAULT 0,"
        " reserved_shipping_minor BIGINT UNSIGNED NOT NULL DEFAULT 0,"
        " currency CHAR(3) NOT NULL, revision BIGINT UNSIGNED NOT NULL DEFAULT 0,"
        " created_at DATETIME(6) NOT NULL, updated_at DATETIME(6) NOT NULL,"
        " PRIMARY KEY (tenant_id, order_id),"
        " KEY ix_refund_guard_active (tenant_id, active_request_id),"
        " CONSTRAINT chk_refund_guard_active CHECK ("
        " (active_request_id IS NULL AND reserved_goods_minor=0"
        " AND reserved_shipping_minor=0) OR active_request_id IS NOT NULL)"
        ")" + _SUFFIX,
    ]
