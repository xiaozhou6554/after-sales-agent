"""步骤05A 单元测试：种子导入映射、迁移 v0002 结构与纯函数校验。

不依赖真实 MySQL；对真实 MySQL 的导入/重置/校验属于步骤05A 的实测验收，
由 python -m bootstrap.mock_data 显式执行并记录于开发运行指南。
"""

from __future__ import annotations

import re
from datetime import date, datetime

from bootstrap.migrate import compute_pending, discover_versions
from src.infrastructure.persistence.mysql.mock_seed_importer import (
    TABLE_SPECS,
    _map_evidence,
    _map_inspection,
    _map_mapping,
    _map_payment_detail,
    _map_snapshot,
    _map_trace,
    compare_counts,
    expected_row_counts,
)

# ---------- 导入顺序：父表必须先于引用它的子表 ----------


def test_import_order_parents_before_children() -> None:
    order = [spec.table for spec in TABLE_SPECS]
    parent_child_pairs = [
        ("mock_sys_tenant", "mock_pim_product"),
        ("mock_pim_product", "mock_pim_product_mapping"),
        ("mock_oms_order", "mock_oms_order_line"),
        ("mock_oms_order", "mock_oms_shipment_batch"),
        ("mock_oms_order", "mock_pay_payment"),
        ("mock_pay_payment", "mock_pay_payment_detail"),
        ("mock_oms_order_line", "mock_pay_payment_detail"),
        ("mock_pay_payment", "mock_pay_refund_execution"),
        ("mock_pay_payment_detail", "mock_pay_refund_detail"),
        ("mock_pay_refund_execution", "mock_pay_refund_detail"),
        ("mock_oms_order_line", "mock_wms_return_authorization"),
        ("mock_wms_return_authorization", "mock_wms_receipt"),
        ("mock_wms_receipt", "mock_wms_inspection"),
        ("mock_pim_product", "mock_proj_metric_snapshot"),
        ("mock_evt_trace", "mock_inv_anomaly_evidence"),
        ("mock_evt_event", "mock_inv_anomaly_evidence"),
        ("mock_proj_metric_snapshot", "mock_inv_anomaly_evidence"),
    ]
    for parent, child in parent_child_pairs:
        assert order.index(parent) < order.index(child), f"{parent} 必须先于 {child} 导入"


def test_every_table_name_has_mock_prefix() -> None:
    for spec in TABLE_SPECS:
        assert spec.table.startswith("mock_"), f"{spec.table} 缺少 mock_ 前缀"


# ---------- 字段映射：金额最小单位、日期解析、可空处理 ----------


def test_payment_detail_maps_minor_units_and_datetime() -> None:
    row = _map_payment_detail(
        {
            "tenant_id": "tenant-north",
            "payment_detail_id": "PAY-DETAIL-001",
            "payment_id": "PAY-BATCH-100",
            "order_id": "ORDER-003",
            "line_id": "LINE-003",
            "product_id": "product-air-01",
            "paid_goods_minor": 90000,
            "paid_shipping_minor": 10000,
            "paid_minor": 100000,
            "paid_at": "2026-09-15T08:00:00Z",
            "detail_sequence": 1,
        }
    )
    assert row["paid_minor"] == 100000  # 最小单位整数，不乘不除
    assert isinstance(row["paid_minor"], int)
    assert row["paid_at"] == datetime(2026, 9, 15, 8, 0, 0)
    assert row["detail_sequence"] == 1


def test_mapping_parses_dates_and_keeps_valid_to_nullable() -> None:
    row = _map_mapping(
        {
            "tenant_id": "tenant-north",
            "source_system": "PIM",
            "source_id": "P-100",
            "version": 2,
            "canonical_product_id": "product-air-01",
            "valid_from": "2026-01-01",
            "valid_to": None,
            "status": "active",
        }
    )
    assert row["valid_from"] == date(2026, 1, 1)
    assert row["valid_to"] is None


def test_ambiguous_mapping_rows_both_preserved() -> None:
    # 种子刻意构造同一 (source_system, source_id, version) 映射两个候选品；
    # 映射为纯函数逐行转换，两行都必须保留（主键含 canonical_product_id）。
    base = {
        "tenant_id": "tenant-north",
        "source_system": "OMS",
        "source_id": "AMB-7",
        "version": 1,
        "valid_from": "2026-01-01",
        "valid_to": None,
        "status": "ambiguous",
    }
    rows = [
        _map_mapping({**base, "canonical_product_id": "product-air-01"}),
        _map_mapping({**base, "canonical_product_id": "product-air-02"}),
    ]
    keys = {(row["source_id"], row["canonical_product_id"]) for row in rows}
    assert keys == {("AMB-7", "product-air-01"), ("AMB-7", "product-air-02")}


def test_inspection_nullable_reason_fields() -> None:
    row = _map_inspection(
        {
            "tenant_id": "tenant-north",
            "inspection_id": "INSPECT-001",
            "receipt_id": "RECEIPT-001",
            "passed_quantity": 10,
            "rejected_quantity": 4,
            "reason_code": None,
            "reason_version": None,
            "confirmed_by": "warehouse-user-01",
        }
    )
    assert row["reason_code"] is None
    assert row["reason_version"] is None


def test_trace_spans_serialized_and_evidence_content_text() -> None:
    trace = _map_trace(
        {
            "tenant_id": "tenant-north",
            "trace_id": "TRACE-RETURN-001",
            "spans": ["api.request", "wms.projection"],
            "status": "ok",
        }
    )
    # JSON 列以序列化字符串写入（驱动会把 list 展开为多值占位符）。
    assert trace["spans"] == '["api.request", "wms.projection"]'

    evidence = _map_evidence(
        {
            "tenant_id": "tenant-north",
            "evidence_id": "EVID-1",
            "source_system": "WMS",
            "source_record_id": "WMS-REC-1",
            "trace_id": "TRACE-RETURN-001",
            "event_id": "WMS-EVT-0800",
            "snapshot_id": "METRIC-RETURN-2026-08",
            "kind": "baseline_fact",
            "content": "基线品退回4件",
        }
    )
    assert evidence["content"] == "基线品退回4件"


def test_snapshot_rate_and_window_fields() -> None:
    row = _map_snapshot(
        {
            "tenant_id": "tenant-north",
            "snapshot_id": "METRIC-RETURN-2026-08",
            "metric_id": "return_passed_item_rate",
            "metric_version": 1,
            "product_id": "product-air-01",
            "cohort": "signed-2026-08",
            "observation_start": "2026-08-01",
            "observation_end": "2026-09-01",
            "numerator": 4,
            "denominator": 100,
            "rate": 0.04,
            "min_sample": 100,
            "source_watermark": "WMS-EVT-0800",
            "quality": "complete",
        }
    )
    assert row["numerator"] == 4
    assert row["denominator"] == 100
    assert row["rate"] == 0.04
    assert row["observation_start"] == date(2026, 8, 1)


# ---------- v0002 迁移：版本发现与 DDL 幂等/外键顺序 ----------


def test_v0002_discovered_and_pending_after_0001() -> None:
    modules = discover_versions()
    versions = [module.VERSION for module in modules]
    # v0003 审计表属于步骤08；本测试只锁定 v0002 在序列中的位置。
    assert versions[:2] == ["0001", "0002"]
    assert compute_pending(versions, ["0001"]) == versions[1:]


def test_v0002_statements_idempotent_and_fk_order_valid() -> None:
    from migrations.versions import v0002_mock_business_tables as v0002

    statements = v0002.statements()
    assert len(statements) >= 17
    created: list[str] = []
    referenced: list[str] = []
    for statement in statements:
        assert statement.startswith("CREATE TABLE IF NOT EXISTS mock_"), statement[:60]
        assert "DROP " not in statement
        table = re.match(r"CREATE TABLE IF NOT EXISTS (\w+)", statement).group(1)
        created.append(table)
        referenced.extend(re.findall(r"REFERENCES (\w+)", statement))
    for ref in referenced:
        assert ref in created, f"外键引用的 {ref} 未在本迁移中创建"
    # 建表顺序：被引用表先于引用表创建
    for index, statement in enumerate(statements):
        for ref in re.findall(r"REFERENCES (\w+)", statement):
            table = re.match(r"CREATE TABLE IF NOT EXISTS (\w+)", statement).group(1)
            assert created.index(ref) < index, f"{ref} 必须先于 {table} 创建"


# ---------- 纯函数校验 ----------


def test_expected_row_counts_uses_seed_array_lengths() -> None:
    data = {
        spec.array_key: [{"n": i} for i in range(index + 1)]
        for index, spec in enumerate(TABLE_SPECS)
    }
    counts = expected_row_counts(data)
    assert counts["mock_sys_tenant"] == 1
    assert counts[TABLE_SPECS[-1].table] == len(TABLE_SPECS)


def test_compare_counts_reports_only_mismatched_tables() -> None:
    expected = {"mock_sys_tenant": 2, "mock_pim_product": 13}
    actual_ok = {"mock_sys_tenant": 2, "mock_pim_product": 13}
    assert compare_counts(expected, actual_ok) == []

    actual_bad = {"mock_sys_tenant": 2, "mock_pim_product": 12}
    report = compare_counts(expected, actual_bad)
    assert report == ["mock_pim_product: expected=13 actual=12"]

    actual_missing = {"mock_sys_tenant": 2}
    report = compare_counts(expected, actual_missing)
    assert report == ["mock_pim_product: expected=13 actual=None"]
