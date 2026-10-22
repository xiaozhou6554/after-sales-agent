"""步骤25：报价逐行组成不可变保存，申请前必须核对。"""

VERSION = "0014"
_SUFFIX = " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"


def statements() -> list[str]:
    return [
        "CREATE TABLE IF NOT EXISTS refund_quote_line ("
        " tenant_id VARCHAR(64) NOT NULL, quote_id VARCHAR(64) NOT NULL,"
        " line_id VARCHAR(64) NOT NULL, quantity INT UNSIGNED NOT NULL,"
        " goods_minor BIGINT UNSIGNED NOT NULL, shipping_minor BIGINT UNSIGNED NOT NULL,"
        " PRIMARY KEY (tenant_id, quote_id, line_id),"
        " CONSTRAINT fk_refund_quote_line_quote FOREIGN KEY (tenant_id, quote_id)"
        " REFERENCES refund_quote (tenant_id, quote_id),"
        " CONSTRAINT chk_refund_quote_line_quantity CHECK (quantity > 0)"
        ")" + _SUFFIX,
    ]
