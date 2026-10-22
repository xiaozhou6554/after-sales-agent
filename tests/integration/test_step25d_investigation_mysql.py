"""步骤25D 集成验收：异常追查固定快照重放与只增修订（FR-24）。

覆盖 TC-107 scope 隔离、TC-110/111/112 贡献按组率差×份额并按贡献排序、
TC-113/114 可比性门停止排名、TC-115 固定快照重放确定性、TC-116/123 修订
只增不改、TC-117/118/121 运行异常发现、TC-119 跨系统冲突列示、TC-120 知识
索引失败列示、TC-122 掩码审计。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from bootstrap.settings import load_settings
from sqlalchemy import text

from application.analytics.projection_service import AnalyticsProjectionService
from application.investigation.service import InvestigationService
from infrastructure.analytics.mysql_projection import MySQLAnalyticsProjectionAdapter
from infrastructure.auth.mock_auth_adapter import MockAuthAdapter
from infrastructure.investigation.mysql_service import MySQLInvestigationAdapter
from infrastructure.ontology.mysql_identity_mapping import MySqlIdentityMapping
from infrastructure.persistence.mysql.connection import create_mysql_engine

_NORTH = "tenant-north"
_SOUTH = "tenant-south"
_AS_OF = date(2026, 10, 15)
_NOW = "2026-10-21 08:00:00"
_SUPERVISOR = SimpleNamespace(
    tenant_id=_NORTH, subject_id="incident-supervisor-north-01"
)


class _Env:
    """25D 环境：两组完整 cohort（订单分组贡献）+ 运行/知识异常夹具。"""

    def __init__(self, engine) -> None:
        self.engine = engine
        self.mapping = MySqlIdentityMapping(engine)
        self.projection = AnalyticsProjectionService(
            MySQLAnalyticsProjectionAdapter(engine, self.mapping)
        )
        self.investigation = InvestigationService(
            MySQLInvestigationAdapter(engine, self.projection)
        )
        self._wipe()
        self._seed()

    def _wipe(self) -> None:
        statements = [
            ("DELETE FROM investigation_revision WHERE tenant_id=:n AND"
             " investigation_id IN (SELECT investigation_id FROM"
             " (SELECT investigation_id FROM investigation WHERE tenant_id=:n"
             " AND created_by IN ('incident-supervisor-north-01',"
             " 'supervisor-north-01')) x)", {"n": _NORTH}),
            ("DELETE FROM investigation WHERE tenant_id=:n AND"
             " created_by IN ('incident-supervisor-north-01',"
             " 'supervisor-north-01')", {"n": _NORTH}),
            ("DELETE FROM analytics_fact WHERE tenant_id IN (:n,:s)",
             {"n": _NORTH, "s": _SOUTH}),
            ("DELETE FROM analytics_quarantine WHERE tenant_id IN (:n,:s)",
             {"n": _NORTH, "s": _SOUTH}),
            ("DELETE FROM analytics_metric_snapshot WHERE tenant_id IN (:n,:s)",
             {"n": _NORTH, "s": _SOUTH}),
            ("DELETE FROM tool_invocation WHERE tenant_id=:n AND"
             " invocation_id LIKE 'AT-TI-%'", {"n": _NORTH}),
            ("DELETE FROM consumer_inbox WHERE tenant_id=:n AND"
             " inbox_id LIKE 'AT-CI-%'", {"n": _NORTH}),
            ("DELETE FROM outbox_event WHERE tenant_id=:n AND"
             " event_id LIKE 'AT-OE-%'", {"n": _NORTH}),
            ("DELETE FROM mock_evt_trace WHERE tenant_id=:n AND"
             " trace_id LIKE 'AT-TR-%'", {"n": _NORTH}),
            ("DELETE FROM knowledge_index_job WHERE tenant_id=:n AND"
             " job_id LIKE 'AT-KJ-%'", {"n": _NORTH}),
            ("DELETE FROM knowledge_release WHERE tenant_id=:n AND"
             " knowledge_release_id LIKE 'AT-KR-%'", {"n": _NORTH}),
            ("DELETE FROM reconciliation_difference WHERE difference_id"
             " LIKE 'AT-DIFF-%'", {}),
            ("DELETE FROM reconciliation_run WHERE run_id LIKE 'AT-RUN-%'", {}),
            ("DELETE FROM refund_execution WHERE execution_id LIKE 'AT-RE-%'",
             {}),
            ("DELETE FROM refund_request WHERE refund_request_id LIKE"
             " 'AT-RR-%'", {}),
            ("DELETE FROM mock_pay_refund_detail WHERE refund_detail_id"
             " LIKE 'AT-RD-%'", {}),
            ("DELETE FROM mock_pay_refund_execution WHERE refund_id"
             " LIKE 'AT-RE-%'", {}),
            ("DELETE FROM mock_pay_payment_detail WHERE payment_detail_id"
             " LIKE 'AT-PD-%'", {}),
            ("DELETE FROM mock_pay_payment WHERE payment_id LIKE 'AT-PAY-%'",
             {}),
            ("DELETE FROM mock_oms_order_line WHERE tenant_id=:n AND"
             " order_id LIKE 'ORDER-AT-%'", {"n": _NORTH}),
            ("DELETE FROM mock_oms_order WHERE order_id LIKE 'ORDER-AT-%'",
             {}),
        ]
        with self.engine.begin() as connection:
            for sql, params in statements:
                connection.execute(text(sql), params)

    def _seed(self) -> None:
        now = datetime(2026, 10, 21, 8, 0, 0)
        rows = []
        for order_id in ("ORDER-AT-IX", "ORDER-AT-IY"):
            rows.append((
                "INSERT INTO mock_oms_order (tenant_id,order_id,customer_id,"
                " status,currency,paid_minor,version) VALUES (:n,:o,'CUST',"
                " 'completed','CNY',100000,1)",
                {"n": _NORTH, "o": order_id},
            ))
        for payment_id, order_id in (
            ("AT-PAY-SEP", "ORDER-AT-IX"), ("AT-PAY-SEP-B", "ORDER-AT-IX"),
        ):
            rows.append((
                "INSERT INTO mock_pay_payment (tenant_id,payment_id,order_id,"
                " paid_minor,currency,status,source_record_id) VALUES (:n,:p,"
                " :o,100000,'CNY','succeeded',:src)",
                {"n": _NORTH, "p": payment_id, "o": order_id,
                 "src": f"SRC-{payment_id}"},
            ))
        with self.engine.begin() as connection:
            for sql, params in rows:
                connection.execute(text(sql), params)

        # 当前窗口 paid-2026-09：订单 IX 80 行（退 40）+ 订单 IY 20 行（退 20）。
        # 基准窗口 paid-2026-08：同两订单 80 行（退 10）+ 20 行（退 4）。
        # 贡献按组匹配基准，两窗口必须使用相同订单标识。
        payment_rows, line_rows = [], []
        plans = (
            ("IX", "ORDER-AT-IX", 80, 40, "AT-PAY-SEP", "2026-09-10 08:00:00"),
            ("IY", "ORDER-AT-IY", 20, 20, "AT-PAY-SEP", "2026-09-10 08:00:00"),
            ("BX", "ORDER-AT-IX", 80, 10, "AT-PAY-SEP-B", "2026-08-10 08:00:00"),
            ("BY", "ORDER-AT-IY", 20, 4, "AT-PAY-SEP-B", "2026-08-10 08:00:00"),
        )
        for tag, order_id, lines, refunds, payment_id, paid_at in plans:
            for index in range(1, lines + 1):
                detail_id = f"AT-PD-{tag}-{index:03d}"
                line_id = f"LINE-{detail_id}"
                payment_rows.append({
                    "t": _NORTH, "d": detail_id, "p": payment_id,
                    "o": order_id, "l": line_id,
                    "paid": paid_at, "seq": index,
                })
                line_rows.append({
                    "t": _NORTH, "o": order_id, "l": line_id,
                })
        with self.engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO mock_oms_order_line (tenant_id,order_id,line_id,"
                " source_product_id,quantity,goods_minor,shipping_minor)"
                " VALUES (:t,:o,:l,'SKU-AIR-02',1,1000,0)"
            ), line_rows)
            connection.execute(text(
                "INSERT INTO mock_pay_payment_detail (tenant_id,"
                " payment_detail_id,payment_id,order_id,line_id,product_id,"
                " paid_goods_minor,paid_shipping_minor,paid_minor,paid_at,"
                " detail_sequence) VALUES (:t,:d,:p,:o,:l,'product-air-02',"
                " 1000,0,1000,:paid,:seq)"
            ), payment_rows)

        execution_rows, refund_detail_rows = [], []
        source_seq = 0
        for tag, order_id, lines, refunds, payment_id, paid_at in plans:
            if refunds == 0:
                continue
            execution_rows.append({
                "t": _NORTH, "r": f"AT-RE-{tag}", "o": order_id,
                "p": payment_id, "amt": refunds * 1000, "now": now,
            })
            for index in range(1, refunds + 1):
                source_seq += 1
                refund_detail_rows.append({
                    "t": _NORTH, "d": f"AT-RD-{tag}-{index:03d}",
                    "pd": f"AT-PD-{tag}-{index:03d}", "r": f"AT-RE-{tag}",
                    "src": f"AT-PSP-{source_seq:04d}",
                })
        with self.engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO mock_pay_refund_execution (tenant_id,refund_id,"
                " order_id,payment_id,amount_minor,currency,status,"
                " idempotency_key,source_record_id,environment,created_at)"
                " VALUES (:t,:r,:o,:p,:amt,'CNY','succeeded',:r,:r,"
                " 'simulation',:now)"
            ), execution_rows)
            connection.execute(text(
                "INSERT INTO mock_pay_refund_detail (tenant_id,"
                " refund_detail_id,payment_detail_id,refund_id,status,"
                " refunded_goods_minor,currency,source_record_id) VALUES"
                " (:t,:d,:pd,:r,'succeeded',1000,'CNY',:src)"
            ), refund_detail_rows)

    def project(self, tenant_id: str, version: int) -> dict:
        return self.projection.project(tenant_id, version, as_of=_AS_OF)

    def snapshot(self, tenant_id: str, version: str, metric_id: str,
                 product_id: str, cohort: str) -> dict | None:
        with self.engine.connect() as connection:
            row = connection.execute(text(
                "SELECT metric_id,product_id,cohort,numerator,denominator,"
                " rate,quality FROM analytics_metric_snapshot"
                " WHERE tenant_id=:t AND projection_version=:v AND metric_id=:m"
                " AND product_id=:p AND cohort=:c"
            ), {"t": tenant_id, "v": version, "m": metric_id, "p": product_id,
                "c": cohort}).mappings().first()
        return dict(row) if row else None

    def add_lines(self, tag: str, order_id: str, count: int,
                  payment_id: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO mock_oms_order_line (tenant_id,order_id,line_id,"
                " source_product_id,quantity,goods_minor,shipping_minor)"
                " VALUES (:t,:o,:l,'SKU-AIR-02',1,1000,0)"
            ), [{
                "t": _NORTH, "o": order_id,
                "l": f"LINE-AT-PD-{tag}-{index:03d}",
            } for index in range(1, count + 1)])
            connection.execute(text(
                "INSERT INTO mock_pay_payment_detail (tenant_id,"
                " payment_detail_id,payment_id,order_id,line_id,product_id,"
                " paid_goods_minor,paid_shipping_minor,paid_minor,paid_at,"
                " detail_sequence) VALUES (:t,:d,:p,:o,:l,'product-air-02',"
                " 1000,0,1000,'2026-09-15 08:00:00',:seq)"
            ), [{
                "t": _NORTH, "d": f"AT-PD-{tag}-{index:03d}", "p": payment_id,
                "o": order_id, "l": f"LINE-AT-PD-{tag}-{index:03d}",
                "seq": 500 + index,
            } for index in range(1, count + 1)])

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
    environment.project(_NORTH, 920)
    yield environment
    environment.close()
    engine.dispose()


# ---------- TC-110：两组完整窗口质量快照 ----------

def test_01_projection_two_complete_windows(env):
    now_row = env.snapshot(_NORTH, "920", "refund_successful_line_rate",
                           "product-air-02", "paid-2026-09")
    assert now_row is not None
    assert now_row["numerator"] == 60
    assert now_row["denominator"] == 100
    assert abs(float(now_row["rate"]) - 0.6) < 1e-9
    assert now_row["quality"] == "complete"
    base_row = env.snapshot(_NORTH, "920", "refund_successful_line_rate",
                            "product-air-02", "paid-2026-08")
    assert base_row is not None
    assert base_row["numerator"] == 14
    assert base_row["denominator"] == 100
    assert abs(float(base_row["rate"]) - 0.14) < 1e-9
    assert base_row["quality"] == "complete"


# ---------- TC-110/111/112：开立调查、按贡献排序、证据引用 ----------

def test_02_open_supported_with_contribution_ordering(env):
    result = env.investigation.open_metric_anomaly(
        _SUPERVISOR,
        metric_id="refund_successful_line_rate",
        product_id="product-air-02",
        cohort="paid-2026-09",
        baseline_cohort="paid-2026-08",
        threshold_pp="5",
        reason="月度异常复核",
    )
    assert result["cause_status"] == "supported"
    assert result["gate"]["delta_pp"] == pytest.approx(46.0)
    assert result["gate"]["comparable"] is True
    findings = result["findings"]
    assert len(findings) == 2
    # 贡献排序：IX 30pp > IY 16pp；组率排序应为 IY(100%) > IX(50%)，二者不同。
    assert findings[0]["cause_code"] == "order:ORDER-AT-IX"
    assert findings[0]["contribution"] == pytest.approx(30.0)
    assert findings[1]["cause_code"] == "order:ORDER-AT-IY"
    assert findings[1]["contribution"] == pytest.approx(16.0)
    assert all("相关性证据" in finding["explanation"] for finding in findings)
    assert all(finding["evidence_refs"] for finding in findings)

    explained = env.investigation.explain(
        _SUPERVISOR, result["investigation_id"], revision=None
    )
    assert set(explained) == {
        "investigation_id", "revision", "anomaly_type", "cause_status",
        "findings", "quality_issues", "metric_version", "ontology_version",
        "mapping_version", "snapshot_id", "evidence_refs",
    }
    assert explained["quality_issues"] == []
    assert explained["anomaly_type"] == "business_metric"
    assert explained["snapshot_id"]
    assert any(ref.startswith("snapshot:") for ref in explained["evidence_refs"])


# ---------- TC-113/114：可比性门停止排名 ----------

def test_03_gate_blocks_ranking(env):
    # 当前窗口 immature（paid-2026-10 未满月）→ 停止排名。
    blocked = env.investigation.open_metric_anomaly(
        _SUPERVISOR,
        metric_id="refund_goods_amount_rate",
        product_id="product-air-01",
        cohort="paid-2026-10",
        baseline_cohort="paid-2026-08",
        reason=" immature gate",
    )
    assert blocked["cause_status"] == "data_quality_blocked"
    assert blocked["findings"] == []
    assert "current_quality_immature" in blocked["gate"]["issues"]
    # 基准窗口缺失 → 同样停止排名。
    missing = env.investigation.open_metric_anomaly(
        _SUPERVISOR,
        metric_id="refund_successful_line_rate",
        product_id="product-air-02",
        cohort="paid-2026-09",
        baseline_cohort="paid-2026-05",
        reason="baseline missing gate",
    )
    assert missing["cause_status"] == "data_quality_blocked"
    assert missing["findings"] == []
    assert "baseline_snapshot_missing" in missing["gate"]["issues"]


# ---------- TC-115：固定快照重放确定性 ----------

def test_04_replay_deterministic_and_denied_cross_tenant(env):
    head = next(
        row for row in env.investigation.list_investigations(_NORTH)
        if row["subject"] == "refund_successful_line_rate:product-air-02:paid-2026-09"
        and row["created_by"] == "incident-supervisor-north-01"
    )
    investigation_id = head["investigation_id"]
    first = env.investigation.replay(_SUPERVISOR, investigation_id, 1)
    assert first["matches"] is True
    assert first["basis_snapshot_version"]
    second = env.investigation.replay(_SUPERVISOR, investigation_id, 1)
    assert second == first
    # 跨租户解释与重放按统一 404 语义拒绝。
    south_auth = SimpleNamespace(tenant_id=_SOUTH, subject_id="supervisor-south")
    with pytest.raises(Exception) as excinfo:
        env.investigation.explain(south_auth, investigation_id, revision=1)
    assert "RESOURCE_UNAVAILABLE" in str(getattr(excinfo.value, "error_code", "")) or \
        "不存在" in str(excinfo.value)


# ---------- TC-116/123：修订只增不改 ----------

def test_05_revise_appends_and_preserves_history(env):
    target = next(
        row for row in env.investigation.list_investigations(_NORTH)
        if row["subject"] == "refund_successful_line_rate:product-air-02:paid-2026-09"
        and row["created_by"] == "incident-supervisor-north-01"
    )
    investigation_id = target["investigation_id"]
    revision_1 = env.investigation.get_revision(_NORTH, investigation_id, 1)
    assert revision_1["basis_snapshot_version"] == "920"

    # 新增 100 行无退款支付明细后按新版本重算并修订。
    env.add_lines("IX2", "ORDER-AT-IX", 100, "AT-PAY-SEP")
    env.project(_NORTH, 921)
    revised = env.investigation.revise(
        _SUPERVISOR, investigation_id,
        actor_subject_id="incident-supervisor-north-01", reason="数据补充后复核",
    )
    assert revised["revision"] == 2
    assert revised["gate"]["delta_pp"] == pytest.approx(16.0)
    assert revised["cause_status"] == "supported"
    top = revised["findings"][0]
    assert top["cause_code"] == "order:ORDER-AT-IX"
    assert top["contribution"] == pytest.approx(8.75)

    head = env.investigation.get_revision(_NORTH, investigation_id, None)
    assert head["revision"] == 2
    assert head["basis_snapshot_version"] == "921"
    preserved = env.investigation.get_revision(_NORTH, investigation_id, 1)
    assert preserved["findings"] == revision_1["findings"]
    assert preserved["basis_snapshot_version"] == "920"
    assert preserved["created_at"] == revision_1["created_at"]


# ---------- TC-117/118/121：运行异常发现 ----------

def test_06_runtime_detection(env):
    now = datetime(2026, 10, 21, 8, 0, 0)
    with env.engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO tool_invocation (tenant_id,invocation_id,request_id,"
            " agent_id,tool_name,normalized_args_digest,args_summary,"
            " access_mode,attempt_count,retry_count,status,result_summary,"
            " error,revision,created_at,updated_at) VALUES"
            " (:t,'AT-TI-T1','AT-REQ-T1','agent-01','query_tracking',"
            " :digest,'{}','read',3,2,'timeout',NULL,'channel timeout',0,"
            " :now,:now),"
            " (:t,'AT-TI-B1','AT-REQ-B1','agent-01','create_refund_request',"
            " :digest2,'{}','write',1,0,'failed',NULL,'business rejected',0,"
            " :now,:now),"
            " (:t,'AT-TI-OK','AT-REQ-OK','agent-01','query_order',"
            " :digest3,'{}','read',1,0,'succeeded','{}',NULL,0,:now,:now)"
        ), {"t": _NORTH, "digest": "a" * 64, "digest2": "b" * 64,
            "digest3": "c" * 64, "now": now})
        connection.execute(text(
            "INSERT INTO outbox_event (tenant_id,event_id,owner_module,"
            " event_type,event_version,schema_version,request_id,"
            " correlation_id,trace_id,actor_subject_id,aggregate_type,"
            " aggregate_id,idempotency_key,payload,status,next_retry_at,"
            " created_at) VALUES (:t,'AT-OE-1','analytics','ontology.x',1,"
            " '1.0','AT-REQ-O1','AT-COR-O1','AT-TR-O1','svc','ontology',"
            " 'AT-AGG-1','AT-IDEM-OE-1','{}','pending',:now,:now)"
        ), {"t": _NORTH, "now": now})
        connection.execute(text(
            "INSERT INTO consumer_inbox (tenant_id,inbox_id,consumer_name,"
            " event_id,event_type,idempotency_key,request_digest,status,"
            " attempts,error) VALUES"
            " (:t,'AT-CI-F1','proj','AT-EV-1','ontology.x','AT-IDEM-CI-1',"
            " :d1,'failed',3,'downstream error'),"
            " (:t,'AT-CI-D1','proj','AT-EV-2','ontology.x','AT-IDEM-DUP',"
            " :d2,'succeeded',1,NULL),"
            " (:t,'AT-CI-D2','proj','AT-EV-3','ontology.x','AT-IDEM-DUP',"
            " :d3,'succeeded',1,NULL)"
        ), {"t": _NORTH, "d1": "1" * 64, "d2": "2" * 64, "d3": "3" * 64})
        connection.execute(text(
            "INSERT INTO mock_evt_trace (tenant_id,trace_id,spans,status)"
            " VALUES (:t,'AT-TR-O1','[]','ok')"
        ), {"t": _NORTH})
    report = env.investigation.detect_runtime_anomalies(_SUPERVISOR)
    failures = {item["dependency"]: item for item in
                report["results"]["dependency_failures"]}
    assert failures["query_tracking"]["classification"] == "timeout"
    assert failures["query_tracking"]["occurrences"] == 1
    assert failures["create_refund_request"]["classification"] == "business_rejection"
    assert "query_order" not in failures
    assert report["results"]["outbox_backlog"].get("pending", 0) >= 1
    assert report["results"]["inbox_failed"] >= 1
    assert report["results"]["inbox_duplicate_keys"] >= 1
    assert report["results"]["trace_coverage_ratio"] is not None
    assert report["results"]["trace_coverage_ratio"] < 1.0
    assert "telemetry_coverage_gap" in report["quality_issues"]
    assert "不编造根因" in report["statement"]


# ---------- TC-119：跨系统冲突列示 ----------

def test_07_cross_system_conflicts(env):
    now = datetime(2026, 10, 21, 8, 0, 0)
    with env.engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO reconciliation_run (tenant_id,run_id,merchant_id,"
            " currency,window_start,window_end,status,executed_count,"
            " executed_minor,channel_count,channel_minor,difference_count,"
            " environment,created_at) VALUES (:t,'AT-RUN-925','MOCK-M1',"
            " 'CNY',:ws,:we,'difference',1,100,1,101,1,'simulation',:now)"
        ), {"t": _NORTH, "ws": now, "we": now, "now": now})
        connection.execute(text(
            "INSERT INTO reconciliation_difference (tenant_id,difference_id,"
            " run_id,difference_type,execution_id,channel_ref,amount_minor,"
            " detail,environment,created_at) VALUES (:t,'AT-DIFF-925',"
            " 'AT-RUN-925','amount_mismatch',NULL,'CH-925',1,"
            " '{\"marker\": \"AT-RECON-925\"}','simulation',:now)"
        ), {"t": _NORTH, "now": now})
    try:
        report = env.investigation.detect_cross_system_conflicts(_SUPERVISOR)
        diffs = {row["difference_id"]: row
                 for row in report["results"]["reconciliation_differences"]}
        assert diffs["AT-DIFF-925"]["difference_type"] == "amount_mismatch"
        assert diffs["AT-DIFF-925"]["execution_id"] is None
        mappings = report["results"]["non_active_mappings"]
        assert mappings, "必须列示非 active 映射事实"
        assert report["owners"]["mappings"] == "PIM"
        assert "不自动冲销" in report["statement"]
    finally:
        with env.engine.begin() as connection:
            connection.execute(text(
                "DELETE FROM reconciliation_difference WHERE difference_id="
                "'AT-DIFF-925'"), {})
            connection.execute(text(
                "DELETE FROM reconciliation_run WHERE run_id='AT-RUN-925'"), {})


# ---------- TC-120：知识索引失败列示 ----------

def test_08_knowledge_anomalies(env):
    with env.engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO knowledge_release (tenant_id,knowledge_release_id,"
            " version,manifest_digest,status,is_active,published_at,"
            " failure_reason) VALUES (:t,'AT-KR-925','v2.1-at',:digest,"
            " 'failed',0,NULL,'index backend unavailable')"
        ), {"t": _NORTH, "digest": "d" * 64})
        connection.execute(text(
            "INSERT INTO knowledge_index_job (tenant_id,job_id,"
            " knowledge_release_id,backend,manifest_digest,status,attempts,"
            " error) VALUES (:t,'AT-KJ-925','AT-KR-925','bm25',:digest,"
            " 'failed',3,'embedding timeout')"
        ), {"t": _NORTH, "digest": "d" * 64})
    try:
        report = env.investigation.detect_knowledge_anomalies(_SUPERVISOR)
        jobs = {row["job_id"]: row for row in report["results"]["failed_index_jobs"]}
        assert jobs["AT-KJ-925"]["error"] == "embedding timeout"
        releases = {row["knowledge_release_id"]: row
                    for row in report["results"]["inactive_releases"]}
        assert releases["AT-KR-925"]["failure_reason"] == "index backend unavailable"
        assert "不得表述为模型根因" in report["statement"]
    finally:
        with env.engine.begin() as connection:
            connection.execute(text(
                "DELETE FROM knowledge_index_job WHERE job_id='AT-KJ-925'"), {})
            connection.execute(text(
                "DELETE FROM knowledge_release WHERE knowledge_release_id="
                "'AT-KR-925'"), {})


# ---------- TC-107：HTTP scope 隔离 ----------

def _client():
    from bootstrap.api_main import create_app
    from fastapi.testclient import TestClient

    app = create_app(settings=load_settings())
    return TestClient(app)


def test_09_http_scope_isolation_and_open(env):
    client = _client()
    with client:
        login = client.post(
            "/auth/mock/session", json={"account_id": "svc-analytics-north-01"}
        )
        assert login.status_code == 200
        reader = {"Authorization": f"Bearer {login.json()['data']['session_token']}"}
        denied = client.post(
            "/workbench/investigations/metric-anomalies",
            json={
                "metric_id": "refund_successful_line_rate",
                "product_id": "product-air-02",
                "cohort": "paid-2026-09",
                "baseline_cohort": "paid-2026-08",
            },
            headers=reader,
        )
        # 项目约定：未授权统一 404 + RESOURCE_UNAVAILABLE。
        assert denied.status_code == 404
        assert denied.json()["error"]["error_code"] == "RESOURCE_UNAVAILABLE"

        login = client.post(
            "/auth/mock/session", json={"account_id": "svc-supervisor-north-01"}
        )
        supervisor = {"Authorization": f"Bearer {login.json()['data']['session_token']}"}
        opened = client.post(
            "/workbench/investigations/metric-anomalies",
            json={
                "metric_id": "refund_successful_line_rate",
                "product_id": "product-air-02",
                "cohort": "paid-2026-09",
                "baseline_cohort": "paid-2026-08",
                "threshold_pp": "5",
                "reason": "TC-107 工作台开立",
            },
            headers=supervisor,
        )
        assert opened.status_code == 200
        body = opened.json()["data"]
        investigation_id = body["investigation_id"]

        # 只有 analytics:read 的读者不能解释（需同时 incident:read）。
        denied_explain = client.get(
            f"/workbench/investigations/{investigation_id}", headers=reader
        )
        assert denied_explain.status_code == 404
        assert denied_explain.json()["error"]["error_code"] == "RESOURCE_UNAVAILABLE"

        # supervisor（analytics:read + incident:read）可解释与重放。
        explained = client.get(
            f"/workbench/investigations/{investigation_id}",
            headers=supervisor,
        )
        assert explained.status_code == 200
        assert explained.json()["data"]["cause_status"] == "supported"
        replayed = client.post(
            f"/workbench/investigations/{investigation_id}/replay",
            params={"revision": 1},
            headers=supervisor,
        )
        assert replayed.status_code == 200
        assert replayed.json()["data"]["matches"] is True


# ---------- TC-107/122：MCP explain_anomaly 与掩码审计 ----------

def test_10_mcp_explain_anomaly_scope_and_audit(env):
    from application.common.audit import AuditUseCase
    from infrastructure.observability.audit_repository import MySQLAuditRepository
    from interface.mcp.context_guard import ToolContextGuard
    from interface.mcp.tools import MCPToolDeps, execute_tool_call

    target = next(
        row for row in env.investigation.list_investigations(_NORTH)
        if row["subject"] == "refund_successful_line_rate:product-air-02:paid-2026-09"
        and row["created_by"] == "incident-supervisor-north-01"
    )
    deps = MCPToolDeps(
        auth_gateway=MockAuthAdapter.from_fixture(
            "data/fixtures/mock-identities.json", enabled=True
        ),
        guard=ToolContextGuard("data/contracts/mcp-tools.json"),
        query_order=None,
        query_payment=None,
        audit=AuditUseCase(MySQLAuditRepository(env.engine)),
        investigation=env.investigation,
    )
    grant = deps.auth_gateway.issue_mock_session("svc-investigation-north-01")
    headers = {"authorization": f"Bearer {grant.session_token}"}
    result = execute_tool_call(
        "explain_anomaly",
        {"investigation_id": target["investigation_id"]},
        deps=deps, headers=headers,
    )
    assert result["status"] == "success"
    assert result["data"]["investigation_id"] == target["investigation_id"]
    assert result["data"]["cause_status"] == "supported"
    assert set(result["data"]) == {
        "investigation_id", "revision", "anomaly_type", "cause_status",
        "findings", "quality_issues", "metric_version", "ontology_version",
        "mapping_version", "snapshot_id", "evidence_refs",
    }

    # 只有 analytics:read 的身份被逐次二次授权拒绝（scope 不足）。
    weak = deps.auth_gateway.issue_mock_session("svc-analytics-north-01")
    denied = execute_tool_call(
        "explain_anomaly",
        {"investigation_id": target["investigation_id"]},
        deps=deps,
        headers={"authorization": f"Bearer {weak.session_token}"},
    )
    assert denied["status"] == "error"
    assert denied["error"]["error_code"] == "RESOURCE_UNAVAILABLE"

    # 不存在的调查按统一语义拒绝，不泄露存在性。
    missing = execute_tool_call(
        "explain_anomaly",
        {"investigation_id": "INV-nonexistent-000000"},
        deps=deps, headers=headers,
    )
    assert missing["status"] == "error"
    assert missing["error"]["error_code"] == "RESOURCE_UNAVAILABLE"

    # TC-122：事前意图与事后结果均已落审计（参数只含模型可见字段）。
    with env.engine.connect() as connection:
        rows = connection.execute(text(
            "SELECT action,parameter_summary,status FROM agent_audit_log"
            " WHERE tenant_id=:t AND action='tool.explain_anomaly'"
        ), {"t": _NORTH}).mappings().all()
    assert rows, "explain_anomaly 必须写审计"
    summaries = [json.loads(row["parameter_summary"]) for row in rows]
    assert any(
        summary.get("investigation_id") == target["investigation_id"]
        for summary in summaries
    )
    assert any(row["status"] == "success" for row in rows)
