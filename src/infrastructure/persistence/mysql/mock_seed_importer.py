"""mock-v2 种子数据导入、隔离重置与计数校验适配器。

字段映射以 data/mock/v2/systems.json 的权威形态为准：
- 金额一律最小单位整数（*_minor）；
- 时间戳为 UTC ISO-8601 字符串（如 2026-09-01T08:00:00Z），落库去时区；
- 日期为 ISO 年月日字符串；
- 导入顺序保证外键依赖：租户 → 主数据 → 订单 → 支付 → 仓储 → 指标 → 事件/链路。

表名带 mock_ 前缀（迁移 v0002），与 mysql-core.sql 定义的系统自有业务表区分。
本模块只负责映射与 SQL 执行；种子文件的读取与 CLI 编排在 bootstrap/mock_data.py。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Callable

from sqlalchemy import text
from sqlalchemy.engine import Connection

DATASET_VERSION = "mock-v2"


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ") if value else None


def _parse_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


class _TableSpec:
    __slots__ = ("array_key", "table", "columns", "row_mapper")

    def __init__(
        self,
        array_key: str,
        table: str,
        columns: list[str],
        row_mapper: Callable[[dict], dict],
    ) -> None:
        self.array_key = array_key
        self.table = table
        self.columns = columns
        self.row_mapper = row_mapper


def _map_tenant(r: dict) -> dict:
    return {"tenant_id": r["tenant_id"], "synthetic": 1 if r["synthetic"] else 0}


def _map_product(r: dict) -> dict:
    return {
        "tenant_id": r["tenant_id"],
        "canonical_product_id": r["canonical_product_id"],
        "name": r["name"],
        "active": 1 if r["active"] else 0,
    }


def _map_mapping(r: dict) -> dict:
    return {
        "tenant_id": r["tenant_id"],
        "source_system": r["source_system"],
        "source_id": r["source_id"],
        "version": r["version"],
        "canonical_product_id": r["canonical_product_id"],
        "valid_from": _parse_date(r.get("valid_from")),
        "valid_to": _parse_date(r.get("valid_to")),
        "status": r["status"],
    }


def _map_order(r: dict) -> dict:
    return {
        "tenant_id": r["tenant_id"],
        "order_id": r["order_id"],
        "customer_id": r["customer_id"],
        "status": r["status"],
        "currency": r["currency"],
        "paid_minor": r["paid_minor"],
        "version": r["version"],
    }


def _map_order_line(r: dict) -> dict:
    return {
        "tenant_id": r["tenant_id"],
        "order_id": r["order_id"],
        "line_id": r["line_id"],
        "source_product_id": r["source_product_id"],
        "quantity": r["quantity"],
        "goods_minor": r["goods_minor"],
        "shipping_minor": r["shipping_minor"],
    }


def _map_shipment(r: dict) -> dict:
    return {
        "tenant_id": r["tenant_id"],
        "order_id": r["order_id"],
        "batch_id": r["batch_id"],
        "quantity": r["quantity"],
        "signed_at": _parse_dt(r["signed_at"]),
    }


def _map_payment(r: dict) -> dict:
    return {
        "tenant_id": r["tenant_id"],
        "payment_id": r["payment_id"],
        "order_id": r["order_id"],
        "paid_minor": r["paid_minor"],
        "currency": r["currency"],
        "status": r["status"],
        "source_record_id": r.get("source_record_id"),
    }


def _map_payment_detail(r: dict) -> dict:
    return {
        "tenant_id": r["tenant_id"],
        "payment_detail_id": r["payment_detail_id"],
        "payment_id": r["payment_id"],
        "order_id": r["order_id"],
        "line_id": r["line_id"],
        "product_id": r["product_id"],
        "paid_goods_minor": r["paid_goods_minor"],
        "paid_shipping_minor": r["paid_shipping_minor"],
        "paid_minor": r["paid_minor"],
        "paid_at": _parse_dt(r["paid_at"]),
        "detail_sequence": r["detail_sequence"],
    }


def _map_refund_execution(r: dict) -> dict:
    return {
        "tenant_id": r["tenant_id"],
        "refund_id": r["refund_id"],
        "order_id": r["order_id"],
        "payment_id": r["payment_id"],
        "amount_minor": r["amount_minor"],
        "currency": r["currency"],
        "status": r["status"],
        "idempotency_key": r["idempotency_key"],
        "source_record_id": r.get("source_record_id"),
    }


def _map_refund_detail(r: dict) -> dict:
    return {
        "tenant_id": r["tenant_id"],
        "refund_detail_id": r["refund_detail_id"],
        "payment_detail_id": r["payment_detail_id"],
        "refund_id": r["refund_id"],
        "status": r["status"],
        "refunded_goods_minor": r["refunded_goods_minor"],
        "currency": r["currency"],
        "source_record_id": r.get("source_record_id"),
    }


def _map_rma(r: dict) -> dict:
    return {
        "tenant_id": r["tenant_id"],
        "rma_id": r["rma_id"],
        "order_id": r["order_id"],
        "line_id": r["line_id"],
        "authorized_quantity": r["authorized_quantity"],
        "status": r["status"],
    }


def _map_receipt(r: dict) -> dict:
    return {
        "tenant_id": r["tenant_id"],
        "receipt_id": r["receipt_id"],
        "rma_id": r["rma_id"],
        "received_quantity": r["received_quantity"],
        "inspected_quantity": r["inspected_quantity"],
        "source_record_id": r.get("source_record_id"),
    }


def _map_inspection(r: dict) -> dict:
    return {
        "tenant_id": r["tenant_id"],
        "inspection_id": r["inspection_id"],
        "receipt_id": r["receipt_id"],
        "passed_quantity": r["passed_quantity"],
        "rejected_quantity": r["rejected_quantity"],
        "reason_code": r.get("reason_code"),
        "reason_version": r.get("reason_version"),
        "confirmed_by": r["confirmed_by"],
    }


def _map_snapshot(r: dict) -> dict:
    return {
        "tenant_id": r["tenant_id"],
        "snapshot_id": r["snapshot_id"],
        "metric_id": r["metric_id"],
        "metric_version": r["metric_version"],
        "product_id": r["product_id"],
        "cohort": r["cohort"],
        "observation_start": _parse_date(r["observation_start"]),
        "observation_end": _parse_date(r["observation_end"]),
        "numerator": r["numerator"],
        "denominator": r["denominator"],
        "rate": r["rate"],
        "min_sample": r["min_sample"],
        "source_watermark": r.get("source_watermark"),
        "quality": r["quality"],
    }


def _map_event(r: dict) -> dict:
    return {
        "tenant_id": r["tenant_id"],
        "event_id": r["event_id"],
        "type": r["type"],
        "aggregate_id": r["aggregate_id"],
        "version": r["version"],
        "status": r["status"],
    }


def _map_trace(r: dict) -> dict:
    return {
        "tenant_id": r["tenant_id"],
        "trace_id": r["trace_id"],
        # JSON 列需序列化后写入；驱动会把 list 参数展开为多值占位符。
        "spans": json.dumps(r["spans"], ensure_ascii=False),
        "status": r["status"],
    }


def _map_evidence(r: dict) -> dict:
    return {
        "tenant_id": r["tenant_id"],
        "evidence_id": r["evidence_id"],
        "source_system": r["source_system"],
        "source_record_id": r["source_record_id"],
        "trace_id": r.get("trace_id"),
        "event_id": r.get("event_id"),
        "snapshot_id": r.get("snapshot_id"),
        "kind": r["kind"],
        "content": r["content"],
    }


# 导入顺序即外键依赖顺序（父表在前）。
TABLE_SPECS: list[_TableSpec] = [
    _TableSpec("tenants", "mock_sys_tenant", ["tenant_id", "synthetic"], _map_tenant),
    _TableSpec(
        "products",
        "mock_pim_product",
        ["tenant_id", "canonical_product_id", "name", "active"],
        _map_product,
    ),
    _TableSpec(
        "product_mappings",
        "mock_pim_product_mapping",
        [
            "tenant_id",
            "source_system",
            "source_id",
            "version",
            "canonical_product_id",
            "valid_from",
            "valid_to",
            "status",
        ],
        _map_mapping,
    ),
    _TableSpec(
        "orders",
        "mock_oms_order",
        [
            "tenant_id",
            "order_id",
            "customer_id",
            "status",
            "currency",
            "paid_minor",
            "version",
        ],
        _map_order,
    ),
    _TableSpec(
        "order_lines",
        "mock_oms_order_line",
        [
            "tenant_id",
            "order_id",
            "line_id",
            "source_product_id",
            "quantity",
            "goods_minor",
            "shipping_minor",
        ],
        _map_order_line,
    ),
    _TableSpec(
        "shipment_batches",
        "mock_oms_shipment_batch",
        ["tenant_id", "order_id", "batch_id", "quantity", "signed_at"],
        _map_shipment,
    ),
    _TableSpec(
        "payments",
        "mock_pay_payment",
        [
            "tenant_id",
            "payment_id",
            "order_id",
            "paid_minor",
            "currency",
            "status",
            "source_record_id",
        ],
        _map_payment,
    ),
    _TableSpec(
        "payment_details",
        "mock_pay_payment_detail",
        [
            "tenant_id",
            "payment_detail_id",
            "payment_id",
            "order_id",
            "line_id",
            "product_id",
            "paid_goods_minor",
            "paid_shipping_minor",
            "paid_minor",
            "paid_at",
            "detail_sequence",
        ],
        _map_payment_detail,
    ),
    _TableSpec(
        "refund_executions",
        "mock_pay_refund_execution",
        [
            "tenant_id",
            "refund_id",
            "order_id",
            "payment_id",
            "amount_minor",
            "currency",
            "status",
            "idempotency_key",
            "source_record_id",
        ],
        _map_refund_execution,
    ),
    _TableSpec(
        "refund_details",
        "mock_pay_refund_detail",
        [
            "tenant_id",
            "refund_detail_id",
            "payment_detail_id",
            "refund_id",
            "status",
            "refunded_goods_minor",
            "currency",
            "source_record_id",
        ],
        _map_refund_detail,
    ),
    _TableSpec(
        "return_authorizations",
        "mock_wms_return_authorization",
        [
            "tenant_id",
            "rma_id",
            "order_id",
            "line_id",
            "authorized_quantity",
            "status",
        ],
        _map_rma,
    ),
    _TableSpec(
        "warehouse_receipts",
        "mock_wms_receipt",
        [
            "tenant_id",
            "receipt_id",
            "rma_id",
            "received_quantity",
            "inspected_quantity",
            "source_record_id",
        ],
        _map_receipt,
    ),
    _TableSpec(
        "warehouse_inspections",
        "mock_wms_inspection",
        [
            "tenant_id",
            "inspection_id",
            "receipt_id",
            "passed_quantity",
            "rejected_quantity",
            "reason_code",
            "reason_version",
            "confirmed_by",
        ],
        _map_inspection,
    ),
    _TableSpec(
        "metric_snapshots",
        "mock_proj_metric_snapshot",
        [
            "tenant_id",
            "snapshot_id",
            "metric_id",
            "metric_version",
            "product_id",
            "cohort",
            "observation_start",
            "observation_end",
            "numerator",
            "denominator",
            "rate",
            "min_sample",
            "source_watermark",
            "quality",
        ],
        _map_snapshot,
    ),
    _TableSpec(
        "events",
        "mock_evt_event",
        ["tenant_id", "event_id", "type", "aggregate_id", "version", "status"],
        _map_event,
    ),
    _TableSpec(
        "traces",
        "mock_evt_trace",
        ["tenant_id", "trace_id", "spans", "status"],
        _map_trace,
    ),
    _TableSpec(
        "anomaly_evidence",
        "mock_inv_anomaly_evidence",
        [
            "tenant_id",
            "evidence_id",
            "source_system",
            "source_record_id",
            "trace_id",
            "event_id",
            "snapshot_id",
            "kind",
            "content",
        ],
        _map_evidence,
    ),
]

_MOCK_TABLES: list[str] = [spec.table for spec in TABLE_SPECS]


def expected_row_counts(data: dict) -> dict[str, int]:
    """按导入表顺序返回各表预期行数（来自种子数组长度）。"""
    return {spec.table: len(data[spec.array_key]) for spec in TABLE_SPECS}


def reset_mock_tables(conn: Connection) -> None:
    """清空全部 mock 表。TRUNCATE 隐式提交，属开发/测试专用操作。"""
    conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
    try:
        for table in reversed(_MOCK_TABLES):
            conn.execute(text(f"TRUNCATE TABLE {table}"))
    finally:
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))


def import_mock_seed(conn: Connection, data: dict) -> dict[str, int]:
    """在当前连接/事务内按外键顺序导入全部种子记录，返回各表行数。

    调用方负责事务边界（建议 engine.begin()），任一表失败整体回滚。
    """
    counts: dict[str, int] = {}
    for spec in TABLE_SPECS:
        rows = [spec.row_mapper(record) for record in data[spec.array_key]]
        if rows:
            columns = ", ".join(spec.columns)
            values = ", ".join(f":{name}" for name in spec.columns)
            conn.execute(
                text(f"INSERT INTO {spec.table} ({columns}) VALUES ({values})"),
                rows,
            )
        counts[spec.table] = len(rows)
    return counts


def table_counts(conn: Connection) -> dict[str, int]:
    """读取各 mock 表当前行数。"""
    return {
        table: int(conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())
        for table in _MOCK_TABLES
    }


def compare_counts(expected: dict[str, int], actual: dict[str, int]) -> list[str]:
    """纯函数：比较预期与实际行数，返回不一致描述列表（空列表表示一致）。"""
    mismatches = []
    for table in _MOCK_TABLES:
        want = expected.get(table)
        got = actual.get(table)
        if want != got:
            mismatches.append(f"{table}: expected={want} actual={got}")
    return mismatches
