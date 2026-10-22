"""步骤25C 集成验收：时态商品映射投影与指标快照（FR-20/FR-23）。

覆盖 TC-95 租户隔离/同名异码、TC-96 歧义与缺映射隔离、TC-104 质量门
（zero_denominator/insufficient_sample/immature）、TC-105 重复退款流水去重、
TC-106 水位滞后与对账差异、TC-108 同版本重放幂等与新版本重算保留历史、
发布生命周期（draft→validate→publish→rollback，SHACL 拒绝与 revision 冲突）、
MCP 指标查询审批门（未批准返回 blocked）与工作台路由。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from bootstrap.settings import load_settings
from sqlalchemy import text

from application.analytics.projection_service import AnalyticsProjectionService
from application.ontology.release_service import OntologyReleaseService
from infrastructure.analytics.mysql_projection import MySQLAnalyticsProjectionAdapter
from infrastructure.messaging.event_contract import EventCatalog
from infrastructure.messaging.outbox_repository import OutboxRepository
from infrastructure.ontology.mysql_identity_mapping import MySqlIdentityMapping
from infrastructure.ontology.mysql_release_repository import (
    MySqlOntologyReleaseRepository,
    ReleaseConflict,
)
from infrastructure.ontology.mysql_release_service import MySQLOntologyReleaseAdapter
from infrastructure.persistence.mysql.connection import create_mysql_engine

_NORTH = "tenant-north"
_SOUTH = "tenant-south"
_AS_OF = date(2026, 10, 15)
_NOW = "2026-10-21 08:00:00"


def _frozen_rows(engine) -> list[dict]:
    with engine.connect() as connection:
        rows = connection.execute(text(
            "SELECT tenant_id,metric_id,product_id,cohort,numerator,denominator,"
            " rate,quality FROM mock_proj_metric_snapshot"
        )).mappings().all()
    return [dict(row) for row in rows]


def _snapshot_index(engine, tenant_id: str, version: str) -> dict:
    with engine.connect() as connection:
        rows = connection.execute(text(
            "SELECT metric_id,product_id,cohort,numerator,denominator,rate,"
            " quality,excluded_reasons FROM analytics_metric_snapshot"
            " WHERE tenant_id=:t AND projection_version=:v"
        ), {"t": tenant_id, "v": version}).mappings().all()
    return {
        (row["metric_id"], row["product_id"], row["cohort"]): dict(row)
        for row in rows
    }


def _scalar(engine, sql: str, params: dict | None = None):
    with engine.connect() as connection:
        return connection.execute(text(sql), params or {}).scalar()


class _Env:
    """25C 测试环境：TC 夹具 + 投影/发布服务；按 AT- 前缀清理。"""

    def __init__(self, engine) -> None:
        self.engine = engine
        self.mapping = MySqlIdentityMapping(engine)
        self.projection = AnalyticsProjectionService(
            MySQLAnalyticsProjectionAdapter(engine, self.mapping)
        )
        self._wipe()
        self._seed()

    # ---------- 清理与播种 ----------

    def _wipe(self) -> None:
        statements = [
            ("DELETE FROM analytics_fact WHERE tenant_id IN (:n,:s)",
             {"n": _NORTH, "s": _SOUTH}),
            ("DELETE FROM analytics_quarantine WHERE tenant_id IN (:n,:s)",
             {"n": _NORTH, "s": _SOUTH}),
            ("DELETE FROM analytics_metric_snapshot WHERE tenant_id IN (:n,:s)",
             {"n": _NORTH, "s": _SOUTH}),
            ("DELETE FROM ontology_release WHERE release_id LIKE 'AT-REL-%'",
             {}),
            ("DELETE FROM outbox_event WHERE aggregate_type='ontology_release'"
             " AND aggregate_id LIKE 'AT-REL-%'", {}),
            ("DELETE FROM reconciliation_difference WHERE CAST(detail AS CHAR)"
             " LIKE '%AT-RECON-%'", {}),
            ("DELETE FROM mock_pay_refund_detail WHERE refund_detail_id"
             " LIKE 'AT-RD-%'", {}),
            ("DELETE FROM mock_pay_refund_execution WHERE refund_id"
             " LIKE 'AT-RE-%'", {}),
            ("DELETE FROM refund_execution WHERE execution_id LIKE 'AT-RE-%'", {}),
            ("DELETE FROM refund_request WHERE refund_request_id LIKE 'AT-RR-%'",
             {}),
            ("DELETE FROM mock_pay_payment_detail WHERE payment_detail_id"
             " LIKE 'AT-PD-%'", {}),
            ("DELETE FROM mock_pay_payment WHERE payment_id LIKE 'AT-PAY-%'", {}),
            ("DELETE FROM mock_wms_inspection WHERE inspection_id LIKE 'AT-INS-%'",
             {}),
            ("DELETE FROM mock_wms_receipt WHERE receipt_id LIKE 'AT-RC-%'", {}),
            ("DELETE FROM mock_wms_return_authorization WHERE rma_id LIKE 'AT-RMA-%'",
             {}),
            ("DELETE FROM mock_oms_shipment_batch WHERE batch_id LIKE 'AT-B-%'", {}),
            ("DELETE FROM mock_oms_order_line WHERE order_id LIKE 'ORDER-AT-%'", {}),
            ("DELETE FROM mock_oms_order WHERE order_id LIKE 'ORDER-AT-%'", {}),
            ("DELETE FROM mock_pim_product_mapping WHERE tenant_id=:n"
             " AND source_id IN ('SKU-AIR-02','SKU-ZERO')", {"n": _NORTH}),
            ("DELETE FROM mock_pim_product_mapping WHERE tenant_id=:n"
             " AND source_id='SKU-BROKEN'", {"n": _NORTH}),
            ("DELETE FROM mock_pim_product WHERE tenant_id=:n"
             " AND canonical_product_id='product-air-tc95'", {"n": _NORTH}),
        ]
        with self.engine.begin() as connection:
            for sql, params in statements:
                connection.execute(text(sql), params)

    def _seed(self) -> None:
        statements = [
            # TC 夹具商品与映射（SKU-NOMAP 故意不建映射）
            ("INSERT INTO mock_pim_product (tenant_id,canonical_product_id,name)"
             " VALUES (:n,'product-air-tc95','TC95零分母商品')", {"n": _NORTH}),
            ("INSERT INTO mock_pim_product_mapping (tenant_id,source_system,"
             " source_id,version,canonical_product_id,valid_from,valid_to,status)"
             " VALUES (:n,'OMS','SKU-AIR-02',1,'product-air-02','2026-01-01',"
             " NULL,'active')", {"n": _NORTH}),
            ("INSERT INTO mock_pim_product_mapping (tenant_id,source_system,"
             " source_id,version,canonical_product_id,valid_from,valid_to,status)"
             " VALUES (:n,'OMS','SKU-ZERO',1,'product-air-tc95','2026-01-01',"
             " NULL,'active')", {"n": _NORTH}),
            # air-02 冻结 2/4 行：签收 2026-10 批次 4 件 + 验收通过 2
            ("INSERT INTO mock_oms_order (tenant_id,order_id,customer_id,status,"
             " currency,paid_minor,version) VALUES (:n,'ORDER-AT-A2','CUST',"
             " 'completed','CNY',40000,1)", {"n": _NORTH}),
            ("INSERT INTO mock_oms_order_line (tenant_id,order_id,line_id,"
             " source_product_id,quantity,goods_minor,shipping_minor) VALUES"
             " (:n,'ORDER-AT-A2','LINE-AT-A2','SKU-AIR-02',4,40000,0)",
             {"n": _NORTH}),
            ("INSERT INTO mock_oms_shipment_batch (tenant_id,order_id,batch_id,"
             " quantity,signed_at) VALUES (:n,'ORDER-AT-A2','AT-B-A2',4,"
             " '2026-10-05 08:00:00')", {"n": _NORTH}),
            ("INSERT INTO mock_wms_return_authorization (tenant_id,rma_id,"
             " order_id,line_id,authorized_quantity,status) VALUES (:n,"
             " 'AT-RMA-A2','ORDER-AT-A2','LINE-AT-A2',4,'authorized')",
             {"n": _NORTH}),
            ("INSERT INTO mock_wms_receipt (tenant_id,receipt_id,rma_id,"
             " received_quantity,inspected_quantity,source_record_id) VALUES"
             " (:n,'AT-RC-A2','AT-RMA-A2',4,4,'WMS-RC-AT-A2')", {"n": _NORTH}),
            ("INSERT INTO mock_wms_inspection (tenant_id,inspection_id,receipt_id,"
             " passed_quantity,rejected_quantity,reason_code,reason_version,"
             " confirmed_by) VALUES (:n,'AT-INS-A2','AT-RC-A2',2,2,'SEAL_DEFECT',"
             " 1,'warehouse-user-01')", {"n": _NORTH}),
            # 零分母：批次 0 件 + 验收通过 1（不伪造率值）
            ("INSERT INTO mock_oms_order (tenant_id,order_id,customer_id,status,"
             " currency,paid_minor,version) VALUES (:n,'ORDER-AT-ZERO','CUST',"
             " 'completed','CNY',10000,1)", {"n": _NORTH}),
            ("INSERT INTO mock_oms_order_line (tenant_id,order_id,line_id,"
             " source_product_id,quantity,goods_minor,shipping_minor) VALUES"
             " (:n,'ORDER-AT-ZERO','LINE-AT-ZERO','SKU-ZERO',1,10000,0)",
             {"n": _NORTH}),
            ("INSERT INTO mock_oms_shipment_batch (tenant_id,order_id,batch_id,"
             " quantity,signed_at) VALUES (:n,'ORDER-AT-ZERO','AT-B-ZERO',0,"
             " '2026-09-10 08:00:00')", {"n": _NORTH}),
            ("INSERT INTO mock_wms_return_authorization (tenant_id,rma_id,"
             " order_id,line_id,authorized_quantity,status) VALUES (:n,"
             " 'AT-RMA-ZERO','ORDER-AT-ZERO','LINE-AT-ZERO',1,'authorized')",
             {"n": _NORTH}),
            ("INSERT INTO mock_wms_receipt (tenant_id,receipt_id,rma_id,"
             " received_quantity,inspected_quantity,source_record_id) VALUES"
             " (:n,'AT-RC-ZERO','AT-RMA-ZERO',1,1,'WMS-RC-AT-ZERO')",
             {"n": _NORTH}),
            ("INSERT INTO mock_wms_inspection (tenant_id,inspection_id,receipt_id,"
             " passed_quantity,rejected_quantity,reason_code,reason_version,"
             " confirmed_by) VALUES (:n,'AT-INS-ZERO','AT-RC-ZERO',1,0,"
             " 'SEAL_DEFECT',1,'warehouse-user-01')", {"n": _NORTH}),
            # 未映射 / 已映射 / 歧义（AMB-7 基线映射→air-01+air-02），均在 2026-11
            ("INSERT INTO mock_oms_order (tenant_id,order_id,customer_id,status,"
             " currency,paid_minor,version) VALUES (:n,'ORDER-AT-NOMAP','CUST',"
             " 'completed','CNY',2000,1)", {"n": _NORTH}),
            ("INSERT INTO mock_oms_order_line (tenant_id,order_id,line_id,"
             " source_product_id,quantity,goods_minor,shipping_minor) VALUES"
             " (:n,'ORDER-AT-NOMAP','LINE-AT-NOMAP','SKU-NOMAP',2,2000,0)",
             {"n": _NORTH}),
            ("INSERT INTO mock_oms_shipment_batch (tenant_id,order_id,batch_id,"
             " quantity,signed_at) VALUES (:n,'ORDER-AT-NOMAP','AT-B-NOMAP',2,"
             " '2026-11-05 08:00:00')", {"n": _NORTH}),
            ("INSERT INTO mock_oms_order (tenant_id,order_id,customer_id,status,"
             " currency,paid_minor,version) VALUES (:n,'ORDER-AT-QMAP','CUST',"
             " 'completed','CNY',3000,1)", {"n": _NORTH}),
            ("INSERT INTO mock_oms_order_line (tenant_id,order_id,line_id,"
             " source_product_id,quantity,goods_minor,shipping_minor) VALUES"
             " (:n,'ORDER-AT-QMAP','LINE-AT-QMAP','SKU-100',3,3000,0)",
             {"n": _NORTH}),
            ("INSERT INTO mock_oms_shipment_batch (tenant_id,order_id,batch_id,"
             " quantity,signed_at) VALUES (:n,'ORDER-AT-QMAP','AT-B-QMAP',3,"
             " '2026-11-08 08:00:00')", {"n": _NORTH}),
            ("INSERT INTO mock_oms_order (tenant_id,order_id,customer_id,status,"
             " currency,paid_minor,version) VALUES (:n,'ORDER-AT-AMB','CUST',"
             " 'completed','CNY',2000,1)", {"n": _NORTH}),
            ("INSERT INTO mock_oms_order_line (tenant_id,order_id,line_id,"
             " source_product_id,quantity,goods_minor,shipping_minor) VALUES"
             " (:n,'ORDER-AT-AMB','LINE-AT-AMB','AMB-7',2,2000,0)",
             {"n": _NORTH}),
            ("INSERT INTO mock_oms_shipment_batch (tenant_id,order_id,batch_id,"
             " quantity,signed_at) VALUES (:n,'ORDER-AT-AMB','AT-B-AMB',2,"
             " '2026-11-03 08:00:00')", {"n": _NORTH}),
            # TC-105：一条支付明细 + 两条同 source_record_id 的成功退款明细
            ("INSERT INTO mock_oms_order (tenant_id,order_id,customer_id,status,"
             " currency,paid_minor,version) VALUES (:n,'ORDER-AT-PD','CUST',"
             " 'completed','CNY',50000,1)", {"n": _NORTH}),
            ("INSERT INTO mock_oms_order_line (tenant_id,order_id,line_id,"
             " source_product_id,quantity,goods_minor,shipping_minor) VALUES"
             " (:n,'ORDER-AT-PD','LINE-AT-PD','SKU-AIR-02',1,50000,0)",
             {"n": _NORTH}),
            ("INSERT INTO mock_pay_payment (tenant_id,payment_id,order_id,"
             " paid_minor,currency,status,source_record_id) VALUES (:n,"
             " 'AT-PAY-DUP','ORDER-AT-PD',50000,'CNY','succeeded',"
             " 'SRC-AT-PAY-DUP')", {"n": _NORTH}),
            ("INSERT INTO mock_pay_payment_detail (tenant_id,payment_detail_id,"
             " payment_id,order_id,line_id,product_id,paid_goods_minor,"
             " paid_shipping_minor,paid_minor,paid_at,detail_sequence) VALUES"
             " (:n,'AT-PD-DUP','AT-PAY-DUP','ORDER-AT-PD','LINE-AT-PD',"
             " 'product-air-02',50000,0,50000,'2026-10-20 08:00:00',1)",
             {"n": _NORTH}),
            ("INSERT INTO mock_pay_refund_execution (tenant_id,refund_id,"
             " order_id,payment_id,amount_minor,currency,status,"
             " idempotency_key,source_record_id,environment,created_at) VALUES"
             " (:n,'AT-RE-DUP','ORDER-AT-PD','AT-PAY-DUP',50000,'CNY',"
             " 'succeeded','AT-IDEM-RE-DUP','SRC-AT-RE-DUP','simulation',:now)",
             {"n": _NORTH, "now": _NOW}),
            ("INSERT INTO mock_pay_refund_detail (tenant_id,refund_detail_id,"
             " payment_detail_id,refund_id,status,refunded_goods_minor,currency,"
             " source_record_id) VALUES (:n,'AT-RD-DUP-1','AT-PD-DUP',"
             " 'AT-RE-DUP','succeeded',50000,'CNY','AT-PSP-DUP')", {"n": _NORTH}),
            ("INSERT INTO mock_pay_refund_detail (tenant_id,refund_detail_id,"
             " payment_detail_id,refund_id,status,refunded_goods_minor,currency,"
             " source_record_id) VALUES (:n,'AT-RD-DUP-2','AT-PD-DUP',"
             " 'AT-RE-DUP','succeeded',50000,'CNY','AT-PSP-DUP')", {"n": _NORTH}),
            # 南租户同源码 SKU-100 → product-air-91（同名异码异租户，TC-95）
            ("INSERT INTO mock_oms_order (tenant_id,order_id,customer_id,status,"
             " currency,paid_minor,version) VALUES (:s,'ORDER-AT-S1','CUST',"
             " 'completed','CNY',5000,1)", {"s": _SOUTH}),
            ("INSERT INTO mock_oms_order_line (tenant_id,order_id,line_id,"
             " source_product_id,quantity,goods_minor,shipping_minor) VALUES"
             " (:s,'ORDER-AT-S1','LINE-AT-S1','SKU-100',5,5000,0)",
             {"s": _SOUTH}),
            ("INSERT INTO mock_oms_shipment_batch (tenant_id,order_id,batch_id,"
             " quantity,signed_at) VALUES (:s,'ORDER-AT-S1','AT-B-S1',5,"
             " '2026-09-20 08:00:00')", {"s": _SOUTH}),
        ]
        with self.engine.begin() as connection:
            for sql, params in statements:
                connection.execute(text(sql), params)

    def project(self, tenant_id: str, version: int) -> dict:
        return self.projection.project(tenant_id, version, as_of=_AS_OF)

    def snapshot(self, tenant_id: str, version: str, metric_id: str,
                 product_id: str, cohort: str) -> dict | None:
        with self.engine.connect() as connection:
            row = connection.execute(text(
                "SELECT metric_id,product_id,cohort,numerator,denominator,rate,"
                " quality,excluded_reasons FROM analytics_metric_snapshot"
                " WHERE tenant_id=:t AND projection_version=:v AND metric_id=:m"
                " AND product_id=:p AND cohort=:c"
            ), {"t": tenant_id, "v": version, "m": metric_id, "p": product_id,
                "c": cohort}).mappings().first()
        if row is None:
            return None
        row = dict(row)
        if isinstance(row["excluded_reasons"], str):
            row["excluded_reasons"] = json.loads(row["excluded_reasons"])
        return row

    def close(self) -> None:
        self._wipe()


@pytest.fixture(scope="module")
def env():
    settings = load_settings()
    engine = create_mysql_engine(
        settings.mysql_url,
        settings.mysql_connect_timeout_s,
        settings.mysql_read_timeout_s,
    )
    environment = _Env(engine)
    yield environment
    environment.close()
    engine.dispose()


# ---------- 冻结基线复现（37 行） ----------

def test_01_frozen_baseline_reproduced(env):
    env.project(_NORTH, 901)
    env.project(_SOUTH, 901)
    computed = {
        _NORTH: _snapshot_index(env.engine, _NORTH, "901"),
        _SOUTH: _snapshot_index(env.engine, _SOUTH, "901"),
    }
    frozen = _frozen_rows(env.engine)
    assert len(frozen) == 37
    for row in frozen:
        key = (row["metric_id"], row["product_id"], row["cohort"])
        target = computed[row["tenant_id"]].get(key)
        assert target is not None, f"冻结行缺失计算结果: {row['tenant_id']}{key}"
        assert target["numerator"] == row["numerator"], key
        assert target["denominator"] == row["denominator"], key
        if row["rate"] is None:
            assert target["rate"] is None, key
        else:
            assert target["rate"] is not None
            assert abs(float(target["rate"]) - float(row["rate"])) < 1e-9, key
        assert target["quality"] == row["quality"], key


def test_02_tenant_isolation_same_name_different_code(env):
    # 同名商品：北 air-01 与南 air-91 都叫"模拟净化器 A"，但异码异租户不合并。
    with env.engine.connect() as connection:
        name_rows = connection.execute(text(
            "SELECT CONCAT(tenant_id,':',canonical_product_id),name"
            " FROM mock_pim_product WHERE (tenant_id=:n AND"
            " canonical_product_id='product-air-01') OR (tenant_id=:s AND"
            " canonical_product_id='product-air-91')"
        ), {"n": _NORTH, "s": _SOUTH}).all()
    names = {row[0]: row[1] for row in name_rows}
    assert names["tenant-north:product-air-01"] == "模拟净化器 A"
    assert names["tenant-south:product-air-91"] == "模拟净化器 A"
    # 同源码 SKU-100：北→air-01，南→air-91，各自投影互不可见。
    north_products = {
        row[0] for row in _rows(env.engine,
            "SELECT DISTINCT product_id FROM analytics_metric_snapshot"
            " WHERE tenant_id=:t AND projection_version='901'", {"t": _NORTH})
    }
    south_products = {
        row[0] for row in _rows(env.engine,
            "SELECT DISTINCT product_id FROM analytics_metric_snapshot"
            " WHERE tenant_id=:t AND projection_version='901'", {"t": _SOUTH})
    }
    assert "product-air-91" not in north_products
    assert "product-air-01" not in south_products
    assert "product-air-02" not in south_products
    assert "product-air-01" in north_products
    assert "product-air-91" in south_products


def _rows(engine, sql: str, params: dict | None = None) -> list:
    with engine.connect() as connection:
        return connection.execute(text(sql), params or {}).all()


# ---------- TC-96：歧义与缺映射隔离，不伪造成功事实 ----------

def test_03_ambiguous_and_unmapped_quarantined(env):
    facts = _rows(env.engine,
        "SELECT fact_type,source_record_id,canonical_product_id,mapping_status"
        " FROM analytics_fact WHERE tenant_id=:t AND projection_version='901'"
        " AND source_record_id IN ('AT-B-AMB','AT-B-NOMAP')", {"t": _NORTH})
    by_record = {row[1]: (row[0], row[2], row[3]) for row in facts}
    assert by_record["AT-B-AMB"][1] is None
    assert by_record["AT-B-AMB"][2] == "ambiguous"
    assert by_record["AT-B-NOMAP"][1] is None
    assert by_record["AT-B-NOMAP"][2] == "unmapped"
    quarantines = _rows(env.engine,
        "SELECT reason,source_record_id FROM analytics_quarantine"
        " WHERE tenant_id=:t AND projection_version='901'"
        " AND source_record_id IN ('AT-B-AMB','AT-B-NOMAP')", {"t": _NORTH})
    reasons = {row[1]: row[0] for row in quarantines}
    assert reasons["AT-B-AMB"] == "ambiguous_product_attribution"
    assert reasons["AT-B-NOMAP"] == "missing_mapping"
    # 歧义/未映射数量不得并入任何商品：air-01 signed-2026-11 分母恰为 QMAP 的 3。
    row = env.snapshot(_NORTH, "901", "return_passed_item_rate",
                       "product-air-01", "signed-2026-11")
    assert row is not None
    assert row["denominator"] == 3
    assert row["numerator"] == 0


# ---------- TC-104：质量门优先级 ----------

def test_04_quality_gate_precedence(env):
    # 纯 immature：air-01 paid-2026-10 分母 120000≥100，窗口未满。
    row = env.snapshot(_NORTH, "901", "refund_goods_amount_rate",
                       "product-air-01", "paid-2026-10")
    assert row is not None
    assert row["denominator"] == 120000
    assert row["quality"] == "immature"
    assert "immature" in row["excluded_reasons"]
    # 零分母：率值必须是 NULL，不得伪造 0。
    row = env.snapshot(_NORTH, "901", "return_passed_item_rate",
                       "product-air-tc95", "signed-2026-09")
    assert row is not None
    assert row["numerator"] == 1
    assert row["denominator"] == 0
    assert row["rate"] is None
    assert row["quality"] == "zero_denominator"
    # 样本不足：air-02 退款行 1/1，窗口未满 + 样本不足，取 insufficient_sample。
    row = env.snapshot(_NORTH, "901", "refund_successful_line_rate",
                       "product-air-02", "paid-2026-10")
    assert row is not None
    assert row["numerator"] == 1
    assert row["denominator"] == 1
    assert abs(float(row["rate"]) - 1.0) < 1e-9
    assert row["quality"] == "insufficient_sample"
    assert "immature" in row["excluded_reasons"]
    # 隔离 cohort：原因列表携带 quarantined（优先级仍由样本不足主导）。
    row = env.snapshot(_NORTH, "901", "return_passed_item_rate",
                       "product-air-01", "signed-2026-11")
    assert row is not None
    assert "quarantined" in row["excluded_reasons"]


# ---------- TC-105：重复退款流水去重 ----------

def test_05_duplicate_refund_deduplicated(env):
    duplicate_count = _scalar(env.engine,
        "SELECT COUNT(*) FROM analytics_fact WHERE tenant_id=:t"
        " AND projection_version='901' AND fact_type='refund_line'"
        " AND canonical_product_id='product-air-02'"
        " AND source_record_id='AT-PSP-DUP'", {"t": _NORTH})
    assert duplicate_count == 1
    row = env.snapshot(_NORTH, "901", "refund_goods_amount_rate",
                       "product-air-02", "paid-2026-10")
    assert row is not None
    assert row["numerator"] == 50000
    assert row["denominator"] == 50000
    assert abs(float(row["rate"]) - 1.0) < 1e-9


# ---------- TC-106a：水位滞后（缺源降质，不伪造分子） ----------

def test_06_watermark_lag_and_recovery(env):
    env.project(_NORTH, 902)
    control = env.snapshot(_NORTH, "902", "return_passed_item_rate",
                           "product-air-01", "signed-2026-08")
    assert control["quality"] == "complete"
    assert control["numerator"] == 4
    try:
        with env.engine.begin() as connection:
            connection.execute(text(
                "DELETE FROM mock_wms_inspection"
                " WHERE tenant_id=:t AND inspection_id='INSPECT-BASE'"
            ), {"t": _NORTH})
        env.project(_NORTH, 903)
        lagged = env.snapshot(_NORTH, "903", "return_passed_item_rate",
                              "product-air-01", "signed-2026-08")
        assert lagged["quality"] == "watermark_lag"
        assert "watermark_lag" in lagged["excluded_reasons"]
        assert lagged["numerator"] == 0
        assert lagged["denominator"] == 100
    finally:
        with env.engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO mock_wms_inspection (tenant_id,inspection_id,"
                " receipt_id,passed_quantity,rejected_quantity,reason_code,"
                " reason_version,confirmed_by) VALUES (:t,'INSPECT-BASE',"
                " 'RECEIPT-BASE',4,0,'SEAL_DEFECT',1,'warehouse-user-01')"
            ), {"t": _NORTH})
    env.project(_NORTH, 904)
    recovered = env.snapshot(_NORTH, "904", "return_passed_item_rate",
                             "product-air-01", "signed-2026-08")
    assert recovered["quality"] == "complete"
    assert recovered["numerator"] == 4


# ---------- TC-106b：已归因对账差异只标记退款指标 ----------

def test_07_reconciliation_difference_flags_refund_metrics_only(env):
    now = datetime(2026, 10, 21, 8, 0, 0)
    with env.engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO reconciliation_run (tenant_id,run_id,merchant_id,"
            " currency,window_start,window_end,status,executed_count,"
            " executed_minor,channel_count,channel_minor,difference_count,"
            " environment,created_at) VALUES (:t,'AT-RUN-R1','MOCK-M1','CNY',"
            " :ws,:we,'difference',1,100,1,101,1,'simulation',:now)"
        ), {"t": _NORTH, "ws": now, "we": now, "now": now})
        connection.execute(text(
            "INSERT INTO refund_request (tenant_id,refund_request_id,quote_id,"
            " order_id,actor_subject_id,customer_id,confirmation_id,"
            " idempotency_key,reason_code,eligibility,review_status,"
            " review_revision,amount_minor,goods_minor,shipping_minor,currency,"
            " policy_id,policy_version,order_version,payment_version,created_at,"
            " updated_at) VALUES (:t,'AT-RR-R1','AT-QUOTE-R1','ORDER-003',"
            " 'cust-north-01','CUSTOMER-001','AT-CF-R1','AT-IDEM-RR-R1',"
            " 'quality_issue','eligible','approved',0,100,100,0,'CNY',"
            " 'POLICY-1','2026-10-v1','1','mock-v2',:now,:now)"
        ), {"t": _NORTH, "now": now})
        connection.execute(text(
            "INSERT INTO refund_execution (tenant_id,execution_id,"
            " refund_request_id,order_id,payment_id,amount_minor,currency,"
            " funds_status,idempotency_key,request_digest,environment,"
            " actor_subject_id,customer_id,created_at,updated_at) VALUES"
            " (:t,'AT-RE-R1','AT-RR-R1','ORDER-003','AT-PAYREF-R1',100,'CNY',"
            " 'succeeded','AT-IDEM-RE-R1',:digest,'simulation',"
            " 'cust-north-01','CUSTOMER-001',:now,:now)"
        ), {"t": _NORTH, "digest": "b" * 64, "now": now})
        connection.execute(text(
            "INSERT INTO reconciliation_difference (tenant_id,difference_id,"
            " run_id,difference_type,execution_id,channel_ref,amount_minor,"
            " detail,environment,created_at) VALUES (:t,'AT-DIFF-R1',"
            " 'AT-RUN-R1','callback_mismatch','AT-RE-R1',NULL,1,"
            " '{\"marker\": \"AT-RECON-ATTRIB\"}','simulation',:now)"
        ), {"t": _NORTH, "now": now})
    try:
        env.project(_NORTH, 905)
        flagged = env.snapshot(_NORTH, "905", "refund_successful_line_rate",
                               "product-air-01", "paid-2026-09")
        assert flagged["quality"] == "reconciliation_difference"
        assert "reconciliation_difference" in flagged["excluded_reasons"]
        flagged_goods = env.snapshot(_NORTH, "905", "refund_goods_amount_rate",
                                     "product-air-01", "paid-2026-09")
        assert flagged_goods["quality"] == "reconciliation_difference"
        # 退货指标不受对账差异影响。
        untouched = env.snapshot(_NORTH, "905", "return_passed_item_rate",
                                 "product-air-01", "signed-2026-09")
        assert untouched["quality"] == "complete"
    finally:
        with env.engine.begin() as connection:
            for sql, params in (
                ("DELETE FROM reconciliation_difference WHERE difference_id="
                 "'AT-DIFF-R1'", {}),
                ("DELETE FROM reconciliation_run WHERE run_id='AT-RUN-R1'", {}),
                ("DELETE FROM refund_execution WHERE execution_id='AT-RE-R1'", {}),
                ("DELETE FROM refund_request WHERE refund_request_id='AT-RR-R1'",
                 {}),
            ):
                connection.execute(text(sql), params)
    env.project(_NORTH, 906)
    restored = env.snapshot(_NORTH, "906", "refund_successful_line_rate",
                            "product-air-01", "paid-2026-09")
    assert restored["quality"] == "complete"


# ---------- TC-108：重放幂等与历史保留 ----------

def test_08_replay_idempotent_and_history_preserved(env):
    first = env.project(_NORTH, 907)
    assert first["facts"] > 0
    second = env.project(_NORTH, 907)
    assert second["facts"] == 0
    assert second["quarantines"] == 0
    before = env.snapshot(_NORTH, "907", "refund_successful_line_rate",
                          "product-air-02", "paid-2026-10")
    assert before["numerator"] == 1
    assert before["denominator"] == 1
    # 新增一条支付明细与成功退款：新版本重算，历史版本不变。
    with env.engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO mock_pay_payment_detail (tenant_id,payment_detail_id,"
            " payment_id,order_id,line_id,product_id,paid_goods_minor,"
            " paid_shipping_minor,paid_minor,paid_at,detail_sequence) VALUES"
            " (:t,'AT-PD-NEW','AT-PAY-DUP','ORDER-AT-PD','LINE-AT-PD',"
            " 'product-air-02',30000,0,30000,'2026-10-21 08:00:00',2)"
        ), {"t": _NORTH})
        connection.execute(text(
            "INSERT INTO mock_pay_refund_detail (tenant_id,refund_detail_id,"
            " payment_detail_id,refund_id,status,refunded_goods_minor,currency,"
            " source_record_id) VALUES (:t,'AT-RD-NEW','AT-PD-NEW','AT-RE-DUP',"
            " 'succeeded',30000,'CNY','AT-PSP-NEW')"
        ), {"t": _NORTH})
    try:
        env.project(_NORTH, 908)
        recomputed = env.snapshot(_NORTH, "908", "refund_successful_line_rate",
                                  "product-air-02", "paid-2026-10")
        assert recomputed["numerator"] == 2
        assert recomputed["denominator"] == 2
        history = env.snapshot(_NORTH, "907", "refund_successful_line_rate",
                               "product-air-02", "paid-2026-10")
        assert history["numerator"] == 1
        assert history["denominator"] == 1
    finally:
        with env.engine.begin() as connection:
            connection.execute(text(
                "DELETE FROM mock_pay_refund_detail WHERE refund_detail_id="
                "'AT-RD-NEW'"), {})
            connection.execute(text(
                "DELETE FROM mock_pay_payment_detail WHERE payment_detail_id="
                "'AT-PD-NEW'"), {})


# ---------- 发布生命周期（draft→validate→publish→rollback） ----------

def _release_service(engine):
    catalog = EventCatalog(
        "data/contracts/event-catalog.json",
        "data/contracts/events.schema.json",
    )
    repository = MySqlOntologyReleaseRepository(
        engine, OutboxRepository(engine, catalog)
    )
    adapter = MySQLOntologyReleaseAdapter(
        engine, repository, MySqlIdentityMapping(engine)
    )
    return OntologyReleaseService(adapter)


def test_09_release_lifecycle_and_shacl_gate(env):
    service = _release_service(env.engine)
    active_before = service.active(_NORTH)
    assert active_before is None  # 测试租户无历史激活，指针从零开始

    draft = service.draft(_NORTH, "AT-REL-R1")
    assert draft["status"] == "candidate"
    manifest = json.loads(draft["manifest_json"])
    assert set(manifest["files"]) == {
        "core.ttl", "constraints.shacl.ttl", "source_mappings.json", "metrics.json"
    }
    assert draft["mapping_version"] == 2  # SKU-100/P-100/ITEM-100 链上最高版本
    assert draft["metric_version"] == 1
    # 发布清单摘要 = 冻结本体四文件 SHA-256（固定顺序拼接）。
    file_digest = hashlib.sha256()
    for name in ("core.ttl", "constraints.shacl.ttl", "source_mappings.json",
                 "metrics.json"):
        file_digest.update(
            Path("data/knowledge/v2/ontology", name).read_bytes()
        )
    assert draft["manifest_digest"] == file_digest.hexdigest()
    for name, digest in manifest["files"].items():
        assert digest == hashlib.sha256(
            Path("data/knowledge/v2/ontology", name).read_bytes()
        ).hexdigest()

    validated = service.validate_release(_NORTH, "AT-REL-R1")
    assert validated["conforms"] is True
    assert service.get(_NORTH, "AT-REL-R1")["status"] == "validated"

    published = service.publish(
        _NORTH, "AT-REL-R1", expected_active_revision=None,
        actor_subject_id="ontology-publisher-north-01",
    )
    assert published["status"] == "active"
    assert published["revision"] == 1
    assert published["activated_at"] is not None

    events = _rows(env.engine,
        "SELECT payload FROM outbox_event WHERE tenant_id=:t"
        " AND aggregate_type='ontology_release' AND aggregate_id='AT-REL-R1'"
        " AND event_type='ontology.release.activated'", {"t": _NORTH})
    assert len(events) == 1
    envelope = events[0][0]
    if isinstance(envelope, str):
        envelope = json.loads(envelope)
    payload = envelope.get("payload", envelope)
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert payload["manifest_digest"] == published["manifest_digest"]
    assert payload["release_id"] == "AT-REL-R1"

    # 旧 revision 期望：冲突且现役指针不变。
    service.draft(_NORTH, "AT-REL-R2")
    service.validate_release(_NORTH, "AT-REL-R2")
    with pytest.raises(ReleaseConflict):
        service.publish(
            _NORTH, "AT-REL-R2", expected_active_revision=0,
            actor_subject_id="ontology-publisher-north-01",
        )
    assert service.active(_NORTH)["release_id"] == "AT-REL-R1"
    assert service.active(_NORTH)["revision"] == 1

    # SHACL 拒绝：未解析映射携带目标 → UnresolvedMappingShape maxCount 0 失败。
    service.draft(_NORTH, "AT-REL-R3")
    with env.engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO mock_pim_product_mapping (tenant_id,source_system,"
            " source_id,version,canonical_product_id,valid_from,valid_to,status)"
            " VALUES (:t,'OMS','SKU-BROKEN',1,'product-air-01','2026-01-01',"
            " NULL,'unmapped')"
        ), {"t": _NORTH})
    try:
        with pytest.raises(ReleaseConflict) as excinfo:
            service.validate_release(_NORTH, "AT-REL-R3")
        assert "SHACL" in str(excinfo.value)
        assert service.active(_NORTH)["release_id"] == "AT-REL-R1"
        assert service.get(_NORTH, "AT-REL-R3")["status"] == "candidate"
    finally:
        with env.engine.begin() as connection:
            connection.execute(text(
                "DELETE FROM mock_pim_product_mapping WHERE tenant_id=:t"
                " AND source_id='SKU-BROKEN'"), {"t": _NORTH})

    service.validate_release(_NORTH, "AT-REL-R3")
    republished = service.publish(
        _NORTH, "AT-REL-R3", expected_active_revision=1,
        actor_subject_id="ontology-publisher-north-01",
    )
    assert republished["release_id"] == "AT-REL-R3"
    assert republished["revision"] == 2
    rolled_back = service.rollback(
        _NORTH, "AT-REL-R1", expected_active_revision=2,
        actor_subject_id="ontology-publisher-north-01",
    )
    assert rolled_back["release_id"] == "AT-REL-R1"
    assert rolled_back["revision"] == 3
    assert service.get(_NORTH, "AT-REL-R3")["status"] == "retired"
    assert service.active(_NORTH)["release_id"] == "AT-REL-R1"


# ---------- MCP 审批门与工作台路由 ----------

def test_10_mcp_query_blocked_without_approval(env):
    auth = SimpleNamespace(tenant_id=_NORTH)
    result = env.projection.query_metrics(
        auth,
        metric_id="return_passed_item_rate",
        cohort_start="2026-08",
        cohort_end="2026-11",
        active_release=None,
    )
    assert result["results"] == []
    assert result["quality"] == "blocked"
    assert result["evidence_refs"] == ["metric:return_passed_item_rate:v1"]


def test_11_workbench_routes(env):
    from bootstrap.api_main import create_app
    from fastapi.testclient import TestClient

    settings = load_settings()
    app = create_app(settings=settings)
    with TestClient(app) as client:
        login = client.post(
            "/auth/mock/session", json={"account_id": "svc-analytics-north-01"}
        )
        assert login.status_code == 200
        reader = {"Authorization":
                  f"Bearer {login.json()['data']['session_token']}"}
        response = client.get(
            "/workbench/analytics/snapshots",
            params={
                "metric_id": "return_passed_item_rate",
                "cohort_start": "2026-08",
                "cohort_end": "2026-11",
            },
            headers=reader,
        )
        assert response.status_code == 200
        results = response.json()["data"]["results"]
        assert results
        assert all("quality" in row and "numerator" in row for row in results)

        denied = client.post(
            "/workbench/analytics/projection/run",
            json={"projection_version": 901, "as_of": "2026-10-15"},
            headers=reader,
        )
        # 项目约定：未授权统一 404 + RESOURCE_UNAVAILABLE（不暴露资源存在性）。
        assert denied.status_code == 404
        assert denied.json()["error"]["error_code"] == "RESOURCE_UNAVAILABLE"

        login = client.post(
            "/auth/mock/session", json={"account_id": "svc-ontology-north-01"}
        )
        publisher = {"Authorization":
                     f"Bearer {login.json()['data']['session_token']}"}
        allowed = client.post(
            "/workbench/analytics/projection/run",
            json={"projection_version": 901, "as_of": "2026-10-15"},
            headers=publisher,
        )
        assert allowed.status_code == 200
        assert allowed.json()["data"]["projection_version"] == 901
