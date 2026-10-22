"""MySQL projection adapter for versioned facts, quarantines, and snapshots."""


from __future__ import annotations

import json
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import text
from sqlalchemy.engine import Engine

from infrastructure.ontology.mysql_identity_mapping import MySqlIdentityMapping
from infrastructure.ontology.shacl_validator import ONTOLOGY_DIR

ENVIRONMENT = "simulation"

QUALITY_PRECEDENCE = (
    "zero_denominator",
    "insufficient_sample",
    "immature",
    "watermark_lag",
    "reconciliation_difference",
    "complete",
)


def combine_quality(reasons: list[str]) -> str:
    if not reasons:
        return "complete"
    for candidate in QUALITY_PRECEDENCE:
        if candidate in reasons:
            return candidate
    return "quarantined"


def _stable_id(*parts) -> str:
    return uuid.uuid5(uuid.NAMESPACE_URL, "|".join(str(p) for p in parts)).hex


class MySQLAnalyticsProjectionAdapter:
    def __init__(self, engine: Engine, identity_mapping: MySqlIdentityMapping) -> None:
        self._engine = engine
        self._mapping = identity_mapping
        self._resolver = None
        metrics_path = ONTOLOGY_DIR / "metrics.json"
        self._metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        self._metrics_by_id = {row["metric_id"]: row for row in self._metrics}

    @property
    def metric_definitions(self) -> dict[str, dict]:
        return self._metrics_by_id

    def project(
        self, tenant_id: str, projection_version: int, *, as_of: date
    ) -> dict:
        version = str(projection_version)
        self._resolver = self._mapping.resolver(tenant_id)
        orders_sources = self._order_sources(tenant_id)
        batches = self._extract_batches(tenant_id, orders_sources)
        inspections = self._extract_inspections(tenant_id)
        payment_lines = self._extract_payment_lines(tenant_id)
        refunds = self._extract_refunds(tenant_id)

        fact_count = 0
        fact_count += self._write_facts(
            tenant_id, version, "shipment_batch", batches
        )
        fact_count += self._write_facts(
            tenant_id, version, "wms_inspection", inspections
        )
        fact_count += self._write_facts(
            tenant_id, version, "payment_line", payment_lines
        )
        fact_count += self._write_facts(
            tenant_id, version, "refund_line", refunds
        )
        quarantine_count = self._write_quarantines(
            tenant_id, version, batches + inspections
        )

        cohorts_signed = self._signed_cohorts(batches, inspections)
        cohorts_paid = self._paid_cohorts(payment_lines, refunds)
        watermarks = self._watermarks(
            batches, inspections, payment_lines, refunds
        )
        prior_watermarks = self._prior_watermarks(tenant_id, projection_version)
        lag_cohorts = self._detect_watermark_lag(watermarks, prior_watermarks)
        diff_cohorts = self._open_reconciliation_cohorts(tenant_id)
        quarantined_signed = self._quarantine_cohorts(tenant_id, version)

        snapshot_rows = self._return_metric_rows(
            tenant_id, version, as_of, cohorts_signed, watermarks, lag_cohorts,
            quarantined_signed,
        )
        snapshot_rows += self._refund_metric_rows(
            tenant_id, version, as_of, cohorts_paid, watermarks, lag_cohorts,
            diff_cohorts,
        )
        self._write_snapshots(snapshot_rows)
        return {
            "tenant_id": tenant_id,
            "projection_version": projection_version,
            "as_of": as_of.isoformat(),
            "facts": fact_count,
            "quarantines": quarantine_count,
            "snapshot_rows": len(snapshot_rows),
            "environment": ENVIRONMENT,
        }

    # ---------- 浜嬪疄鎶藉彇锛堝悇绮掑害鍏堝幓閲嶅啀鍏宠仈锛岄伩鍏嶅瀵瑰鏀惧ぇ锛?----------

    def _order_sources(self, tenant_id: str) -> dict[str, list[str]]:
        with self._engine.connect() as connection:
            rows = connection.execute(text(
                "SELECT DISTINCT order_id, source_product_id FROM mock_oms_order_line"
                " WHERE tenant_id=:t ORDER BY order_id, source_product_id"
            ), {"t": tenant_id}).all()
        sources: dict[str, list[str]] = defaultdict(list)
        for order_id, source_id in rows:
            sources[order_id].append(source_id)
        return dict(sources)

    def _resolve_order(self, tenant_id: str, source_ids: list[str], on_date: date) -> tuple:
        targets: set[str] = set()
        statuses: set[str] = set()
        for source_id in source_ids:
            resolution = self._resolver("OMS", source_id, on_date)
            statuses.add(resolution.status)
            if resolution.canonical_product_id is not None:
                targets.add(resolution.canonical_product_id)
        if len(targets) == 1 and "ambiguous" not in statuses:
            return next(iter(targets)), "active"
        if "ambiguous" in statuses or len(targets) > 1:
            return None, "ambiguous"
        for candidate in ("unmapped", "expired"):
            if candidate in statuses:
                return None, candidate
        return None, "unmapped"

    def _extract_batches(self, tenant_id: str, orders_sources: dict) -> list[dict]:
        with self._engine.connect() as connection:
            rows = connection.execute(text(
                "SELECT batch_id, order_id, quantity, signed_at"
                " FROM mock_oms_shipment_batch WHERE tenant_id=:t"
                " ORDER BY batch_id"
            ), {"t": tenant_id}).mappings().all()
        facts = []
        for row in rows:
            signed_at = row["signed_at"]
            on_date = signed_at.date() if isinstance(signed_at, datetime) else date.today()
            source_ids = orders_sources.get(row["order_id"], [])
            canonical, status = self._resolve_order(tenant_id, source_ids, on_date)
            facts.append({
                "source_system": "OMS",
                "source_record_id": row["batch_id"],
                "source_version": "1",
                "event_time": signed_at,
                "canonical_product_id": canonical,
                "mapping_status": status,
                "order_id": row["order_id"],
                "quantity": int(row["quantity"]),
                "cohort_month": signed_at.strftime("%Y-%m"),
                "dedup_key": row["batch_id"],
            })
        return facts

    def _extract_inspections(self, tenant_id: str) -> list[dict]:
        """Extract and deduplicate warehouse inspections."""
        with self._engine.connect() as connection:
            rows = connection.execute(text(
                "SELECT i.inspection_id, i.passed_quantity, i.rejected_quantity,"
                " agg.order_id, agg.source_product_id, agg.order_signed_at"
                " FROM mock_wms_inspection i"
                " JOIN mock_wms_receipt rc ON rc.tenant_id=i.tenant_id"
                " AND rc.receipt_id=i.receipt_id"
                " JOIN (SELECT ra.tenant_id, ra.rma_id,"
                " MIN(ra.order_id) AS order_id,"
                " MIN(n.source_product_id) AS source_product_id,"
                " MIN(b.signed_at) AS order_signed_at"
                " FROM mock_wms_return_authorization ra"
                " JOIN mock_oms_order_line n ON n.tenant_id=ra.tenant_id"
                " AND n.order_id=ra.order_id AND n.line_id=ra.line_id"
                " JOIN mock_oms_shipment_batch b ON b.tenant_id=ra.tenant_id"
                " AND b.order_id=ra.order_id"
                " GROUP BY ra.tenant_id, ra.rma_id) agg"
                " ON agg.tenant_id=rc.tenant_id AND agg.rma_id=rc.rma_id"
                " WHERE i.tenant_id=:t ORDER BY i.inspection_id"
            ), {"t": tenant_id}).mappings().all()
        facts = []
        for row in rows:
            signed = row["order_signed_at"]
            on_date = signed.date() if isinstance(signed, datetime) else date.today()
            resolution = self._resolver("OMS", row["source_product_id"], on_date)
            facts.append({
                "source_system": "WMS",
                "source_record_id": row["inspection_id"],
                "source_version": "1",
                "event_time": signed,
                "canonical_product_id": resolution.canonical_product_id,
                "mapping_status": resolution.status,
                "order_id": row["order_id"],
                "quantity": int(row["passed_quantity"]),
                "passed_quantity": int(row["passed_quantity"]),
                "rejected_quantity": int(row["rejected_quantity"]),
                "cohort_month": signed.strftime("%Y-%m") if isinstance(signed, datetime) else None,
                "dedup_key": row["inspection_id"],
            })
        return facts

    def _extract_payment_lines(self, tenant_id: str) -> list[dict]:
        with self._engine.connect() as connection:
            rows = connection.execute(text(
                "SELECT payment_detail_id, order_id, product_id, paid_goods_minor,"
                " paid_at FROM mock_pay_payment_detail WHERE tenant_id=:t"
                " ORDER BY payment_detail_id"
            ), {"t": tenant_id}).mappings().all()
        return [
            {
                "source_system": "PAY",
                "source_record_id": row["payment_detail_id"],
                "source_version": "1",
                "event_time": row["paid_at"],
                "canonical_product_id": row["product_id"],
                "mapping_status": "active",
                "order_id": row["order_id"],
                "quantity": 1,
                "goods_minor": int(row["paid_goods_minor"] or 0),
                "cohort_month": row["paid_at"].strftime("%Y-%m"),
                "dedup_key": row["payment_detail_id"],
            }
            for row in rows
        ]

    def _extract_refunds(self, tenant_id: str) -> list[dict]:
        """Deduplicate refund records by source record id."""
        with self._engine.connect() as connection:
            rows = connection.execute(text(
                "SELECT rd.refund_detail_id, rd.source_record_id, rd.status,"
                " rd.refunded_goods_minor, pd.product_id, pd.order_id, pd.paid_at"
                " FROM mock_pay_refund_detail rd"
                " JOIN mock_pay_payment_detail pd ON pd.tenant_id=rd.tenant_id"
                " AND pd.payment_detail_id=rd.payment_detail_id"
                " WHERE rd.tenant_id=:t"
                " ORDER BY rd.source_record_id, rd.refund_detail_id"
            ), {"t": tenant_id}).mappings().all()
        seen: set[str] = set()
        facts = []
        for row in rows:
            if row["source_record_id"] in seen:
                continue
            seen.add(row["source_record_id"])
            facts.append({
                "source_system": "PAY",
                "source_record_id": row["source_record_id"],
                "source_version": "1",
                "event_time": row["paid_at"],
                "canonical_product_id": row["product_id"],
                "mapping_status": "active",
                "order_id": row["order_id"],
                "quantity": 1,
                "goods_minor": int(row["refunded_goods_minor"] or 0),
                "succeeded": row["status"] == "succeeded",
                "cohort_month": row["paid_at"].strftime("%Y-%m"),
                "dedup_key": row["refund_detail_id"],
            })
        return facts

    def _write_facts(
        self, tenant_id: str, version: str, fact_type: str, facts: list[dict]
    ) -> int:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        inserted = 0
        with self._engine.begin() as connection:
            for fact in facts:
                result = connection.execute(text(
                    "INSERT IGNORE INTO analytics_fact (tenant_id, fact_id,"
                    " projection_version, fact_type, source_system,"
                    " source_record_id, source_version, event_time,"
                    " canonical_product_id, mapping_status, cohort_id, quantity,"
                    " goods_minor, dedup_key, revision, payload, evidence,"
                    " environment, created_at)"
                    " VALUES (:t,:fid,:pv,:ft,:ss,:srid,:sv,:et,:cp,:ms,:co,:q,:g,"
                    " :dk,1,:payload,:evidence,:env,:now)"
                ), {
                    "t": tenant_id,
                    "fid": _stable_id(tenant_id, version, fact_type, fact["dedup_key"]),
                    "pv": version,
                    "ft": fact_type,
                    "ss": fact["source_system"],
                    "srid": fact["source_record_id"],
                    "sv": fact["source_version"],
                    "et": fact["event_time"],
                    "cp": fact["canonical_product_id"],
                    "ms": fact["mapping_status"],
                    "co": fact.get("cohort_month"),
                    "q": fact.get("quantity"),
                    "g": fact.get("goods_minor"),
                    "dk": str(fact["dedup_key"])[:191],
                    "payload": json.dumps(
                        {k: v for k, v in fact.items()
                         if k not in ("event_time",)},
                        ensure_ascii=False, default=str,
                    ),
                    "evidence": json.dumps({
                        "source_system": fact["source_system"],
                        "source_record_id": fact["source_record_id"],
                    }),
                    "env": ENVIRONMENT,
                    "now": now,
                })
                inserted += result.rowcount
        return inserted

    def _write_quarantines(
        self, tenant_id: str, version: str, facts: list[dict]
    ) -> int:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        inserted = 0
        with self._engine.begin() as connection:
            for fact in facts:
                status = fact.get("mapping_status")
                if status in ("active", None):
                    continue
                reason = (
                    "ambiguous_product_attribution"
                    if status == "ambiguous"
                    else "missing_mapping"
                )
                result = connection.execute(text(
                    "INSERT IGNORE INTO analytics_quarantine (tenant_id,"
                    " quarantine_id, projection_version, reason, source_system,"
                    " source_record_id, detail, environment, created_at)"
                    " VALUES (:t,:qid,:pv,:r,:ss,:srid,:detail,:env,:now)"
                ), {
                    "t": tenant_id,
                    "qid": _stable_id(
                        tenant_id, version, reason,
                        fact["source_system"], fact["source_record_id"],
                    ),
                    "pv": version,
                    "r": reason,
                    "ss": fact["source_system"],
                    "srid": fact["source_record_id"],
                    "detail": json.dumps(
                        {
                            "order_id": fact.get("order_id"),
                            "mapping_status": status,
                            "cohort_month": fact.get("cohort_month"),
                        },
                        ensure_ascii=False,
                    ),
                    "env": ENVIRONMENT,
                    "now": now,
                })
                inserted += result.rowcount
        return inserted

    # ---------- 鑱氬悎涓庡揩鐓?----------

    def _signed_cohorts(self, batches: list[dict], inspections: list[dict]) -> dict:
        cohorts: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(dict))
        for fact in batches:
            if fact["canonical_product_id"] is None:
                continue
            bucket = cohorts[fact["canonical_product_id"]].setdefault(
                f"signed-{fact['cohort_month']}", {"den": 0}
            )
            bucket["den"] += fact["quantity"]
        for fact in inspections:
            if fact["canonical_product_id"] is None or not fact["cohort_month"]:
                continue
            bucket = cohorts[fact["canonical_product_id"]].setdefault(
                f"signed-{fact['cohort_month']}", {"den": 0}
            )
            bucket["num"] = bucket.get("num", 0) + fact["passed_quantity"]
        return {product: dict(months) for product, months in cohorts.items()}

    def _paid_cohorts(self, payment_lines: list[dict], refunds: list[dict]) -> dict:
        cohorts: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(dict))
        for fact in payment_lines:
            bucket = cohorts[fact["canonical_product_id"]].setdefault(
                f"paid-{fact['cohort_month']}",
                {"den_lines": 0, "den_goods": 0},
            )
            bucket["den_lines"] += 1
            bucket["den_goods"] += fact["goods_minor"]
        for fact in refunds:
            if not fact["succeeded"]:
                continue
            bucket = cohorts[fact["canonical_product_id"]].setdefault(
                f"paid-{fact['cohort_month']}",
                {"den_lines": 0, "den_goods": 0},
            )
            bucket["num_lines"] = bucket.get("num_lines", 0) + 1
            bucket["num_goods"] = bucket.get("num_goods", 0) + fact["goods_minor"]
        return {product: dict(months) for product, months in cohorts.items()}

    def _watermarks(
        self,
        batches: list[dict],
        inspections: list[dict],
        payment_lines: list[dict],
        refunds: list[dict],
    ) -> dict:
        """鎶曞奖鑷湁姘翠綅锛氭寜 cohort 璁板綍鍚勬簮琛屾暟涓庢渶澶т簨浠舵椂闂淬€?"""
        watermarks: dict[str, dict] = defaultdict(dict)

        def add(cohort: str, source: str, count: int, event_time) -> None:
            entry = watermarks[cohort].setdefault(
                source, {"count": 0, "max_event_time": None}
            )
            entry["count"] += count
            if event_time is None:
                return
            normalized = (
                event_time.isoformat() if isinstance(event_time, datetime)
                else str(event_time)
            )
            if entry["max_event_time"] is None or normalized > entry["max_event_time"]:
                entry["max_event_time"] = normalized

        for fact in batches:
            if fact["canonical_product_id"] is None:
                continue
            add(f"signed-{fact['cohort_month']}", "oms_batches", 1, fact["event_time"])
        for fact in inspections:
            if fact["cohort_month"]:
                add(f"signed-{fact['cohort_month']}", "wms_inspections", 1, None)
        for fact in payment_lines:
            add(f"paid-{fact['cohort_month']}", "pay_payment_details", 1, fact["event_time"])
        for fact in refunds:
            add(f"paid-{fact['cohort_month']}", "pay_refund_details", 1, fact["event_time"])
        return dict(watermarks)

    def _prior_watermarks(self, tenant_id: str, projection_version: int) -> dict:
        with self._engine.connect() as connection:
            rows = connection.execute(text(
                "SELECT cohort, source_watermarks FROM analytics_metric_snapshot"
                " WHERE tenant_id=:t"
                " AND CAST(projection_version AS UNSIGNED) < :pv"
                " ORDER BY CAST(projection_version AS UNSIGNED) DESC"
            ), {"t": tenant_id, "pv": projection_version}).mappings().all()
        prior: dict[str, dict] = {}
        for row in rows:
            payload = row["source_watermarks"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            for source, entry in (payload or {}).items():
                existing = prior.setdefault(row["cohort"], {})
                count = int(entry.get("count", 0))
                if count > existing.get(source, {}).get("count", -1):
                    existing[source] = {
                        "count": count,
                        "max_event_time": entry.get("max_event_time"),
                    }
        return prior

    def _detect_watermark_lag(self, watermarks: dict, prior: dict) -> set[str]:
        lagged: set[str] = set()
        for cohort, sources in prior.items():
            current = watermarks.get(cohort, {})
            for source, entry in sources.items():
                current_entry = current.get(source)
                if current_entry is None or current_entry["count"] < entry["count"]:
                    lagged.add(cohort)
        return lagged

    def _open_reconciliation_cohorts(self, tenant_id: str) -> dict:
        """Return paid cohorts affected by unresolved reconciliation differences."""

        with self._engine.connect() as connection:
            differences = connection.execute(text(
                "SELECT execution_id FROM reconciliation_difference"
                " WHERE tenant_id=:t AND execution_id IS NOT NULL"
            ), {"t": tenant_id}).all()
            cohorts: dict[str, set[str]] = defaultdict(set)
            for (execution_id,) in differences:
                rows = connection.execute(text(
                    "SELECT DISTINCT pd.product_id,"
                    " SUBSTRING(pd.paid_at,1,7) AS m"
                    " FROM refund_execution e"
                    " JOIN refund_request r ON r.tenant_id=e.tenant_id"
                    " AND r.refund_request_id=e.refund_request_id"
                    " JOIN mock_pay_payment_detail pd ON pd.tenant_id=e.tenant_id"
                    " AND pd.order_id=r.order_id"
                    " WHERE e.tenant_id=:t AND e.execution_id=:e"
                ), {"t": tenant_id, "e": execution_id}).all()
                for product_id, month in rows:
                    cohorts[f"paid-{month}"].add(product_id)
        return dict(cohorts)

    def _quarantine_cohorts(self, tenant_id: str, version: str) -> set[str]:
        """Find signed cohorts with facts that could not be attributed."""
        with self._engine.connect() as connection:
            rows = connection.execute(text(
                "SELECT detail FROM analytics_quarantine WHERE tenant_id=:t "
                "AND projection_version=:v AND reason IN "
                "('missing_mapping','ambiguous_product_attribution','ambiguous_mapping')"
            ), {"t": tenant_id, "v": version}).all()
        cohorts: set[str] = set()
        for (detail,) in rows:
            if isinstance(detail, str):
                detail = json.loads(detail)
            month = detail.get("cohort_month")
            if month:
                cohorts.add(f"signed-{month}")
        return cohorts

    def _return_metric_rows(
        self, tenant_id: str, version: str, as_of: date, cohorts: dict,
        watermarks: dict, lag_cohorts: set[str], quarantined_signed: set[str],
    ) -> list[dict]:
        metric = self._metrics_by_id["return_passed_item_rate"]
        rows = []
        for product, months in cohorts.items():
            for cohort, bucket in months.items():
                numerator = int(bucket.get("num", 0))
                denominator = int(bucket.get("den", 0))
                rows.append(self._snapshot_row(
                    tenant_id, version, metric, product, cohort, numerator,
                    denominator, as_of, watermarks.get(cohort, {}),
                    cohort in lag_cohorts, False, cohort in quarantined_signed,
                ))
        return rows

    def _refund_metric_rows(
        self, tenant_id: str, version: str, as_of: date, cohorts: dict,
        watermarks: dict, lag_cohorts: set[str], diff_cohorts: dict,
    ) -> list[dict]:
        line_metric = self._metrics_by_id["refund_successful_line_rate"]
        goods_metric = self._metrics_by_id["refund_goods_amount_rate"]
        rows = []
        for product, months in cohorts.items():
            for cohort, bucket in months.items():
                reconciled = product in diff_cohorts.get(cohort, set())
                hit = cohort in lag_cohorts
                for metric, numerator, denominator in (
                    (line_metric, int(bucket.get("num_lines", 0)),
                     int(bucket.get("den_lines", 0))),
                    (goods_metric, int(bucket.get("num_goods", 0)),
                     int(bucket.get("den_goods", 0))),
                ):
                    rows.append(self._snapshot_row(
                        tenant_id, version, metric, product, cohort, numerator,
                        denominator, as_of, watermarks.get(cohort, {}), hit,
                        reconciled, False,
                    ))
        return rows

    def _snapshot_row(
        self, tenant_id: str, version: str, metric: dict, product_id: str,
        cohort: str, numerator: int, denominator: int, as_of: date,
        watermark_entries: dict, lagged: bool, reconciliation: bool,
        quarantined: bool,
    ) -> dict:
        month = cohort.split("-", 1)[1]
        window_start = date.fromisoformat(f"{month}-01")
        next_month = (window_start.replace(day=28) + timedelta(days=4)).replace(day=1)
        window_end = next_month - timedelta(days=1)
        reasons = []
        if denominator == 0:
            reasons.append("zero_denominator")
        if denominator < int(metric.get("minimum_sample", 0)):
            reasons.append("insufficient_sample")
        if (as_of - window_start).days < int(metric.get("window_days", 0)):
            reasons.append("immature")
        if lagged:
            reasons.append("watermark_lag")
        if reconciliation:
            reasons.append("reconciliation_difference")
        if quarantined:
            reasons.append("quarantined")
        rate = None if denominator == 0 else (
            Decimal(numerator) / Decimal(denominator)
        ).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
        return {
            "snapshot_id": _stable_id(
                tenant_id, version, metric["metric_id"], product_id, cohort
            ),
            "tenant_id": tenant_id,
            "projection_version": version,
            "metric_id": metric["metric_id"],
            "metric_version": int(metric["version"]),
            "product_id": product_id,
            "cohort": cohort,
            "window_start": window_start,
            "window_end": window_end,
            "numerator": numerator,
            "denominator": denominator,
            "rate": rate,
            "min_sample": int(metric.get("minimum_sample", 0)),
            "quality": combine_quality(reasons),
            "excluded_reasons": reasons,
            "source_watermarks": watermark_entries,
            "as_of": datetime.combine(as_of, datetime.min.time()),
            "environment": ENVIRONMENT,
        }

    def _write_snapshots(self, rows: list[dict]) -> None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with self._engine.begin() as connection:
            for row in rows:
                connection.execute(text(
                    "INSERT INTO analytics_metric_snapshot (tenant_id, snapshot_id, "
                    "projection_version, metric_id, metric_version, product_id, cohort, "
                    "window_start, window_end, numerator, denominator, rate, min_sample, "
                    "quality, excluded_reasons, source_watermarks, reconciliation_run_id, "
                    "as_of, environment, created_at) VALUES "
                    "(:t,:sid,:pv,:mid,:mv,:pid,:co,:ws,:we,:num,:den,:rate,:ms,:q,:ex,"
                    ":sw,NULL,:ao,:env,:now) ON DUPLICATE KEY UPDATE "
                    "window_start=VALUES(window_start), window_end=VALUES(window_end), "
                    "numerator=VALUES(numerator), denominator=VALUES(denominator), "
                    "rate=VALUES(rate), min_sample=VALUES(min_sample), quality=VALUES(quality), "
                    "excluded_reasons=VALUES(excluded_reasons), "
                    "source_watermarks=VALUES(source_watermarks), as_of=VALUES(as_of)"
                ), {
                    "t": row["tenant_id"], "sid": row["snapshot_id"],
                    "pv": row["projection_version"], "mid": row["metric_id"],
                    "mv": row["metric_version"], "pid": row["product_id"],
                    "co": row["cohort"], "ws": row["window_start"],
                    "we": row["window_end"], "num": row["numerator"],
                    "den": row["denominator"], "rate": row["rate"],
                    "ms": row["min_sample"], "q": row["quality"],
                    "ex": json.dumps(row["excluded_reasons"], ensure_ascii=False),
                    "sw": json.dumps(row["source_watermarks"], ensure_ascii=False, default=str),
                    "ao": row["as_of"], "env": row["environment"], "now": now,
                })

    def raw_snapshots(
        self, tenant_id: str, *, metric_id: str, cohort_start: str, cohort_end: str
    ) -> list[dict]:
        with self._engine.connect() as connection:
            rows = connection.execute(text(
                "SELECT snapshot_id, projection_version, metric_id, metric_version, "
                "product_id, cohort, window_start, window_end, numerator, denominator, "
                "rate, min_sample, quality, excluded_reasons, source_watermarks, as_of, "
                "environment FROM analytics_metric_snapshot WHERE tenant_id=:t "
                "AND metric_id=:m AND SUBSTRING_INDEX(cohort,'-',-2) >= :cs "
                "AND SUBSTRING_INDEX(cohort,'-',-2) <= :ce "
                "ORDER BY CAST(projection_version AS UNSIGNED), cohort, product_id"
            ), {"t": tenant_id, "m": metric_id, "cs": cohort_start,
                "ce": cohort_end}).mappings().all()
        results = []
        for row in rows:
            item = dict(row)
            for key in ("excluded_reasons", "source_watermarks"):
                if isinstance(item[key], str):
                    item[key] = json.loads(item[key])
            for key in ("window_start", "window_end", "as_of"):
                if hasattr(item[key], "isoformat"):
                    item[key] = item[key].isoformat()
            if item.get("rate") is not None:
                item["rate"] = float(item["rate"])
            results.append(item)
        return results

    def query_metrics(
        self,
        auth,
        *,
        metric_id: str,
        cohort_start: str,
        cohort_end: str,
        active_release: dict | None,
    ) -> dict:
        """鎸夊凡鍙戝竷鎸囨爣涓庢巿鏉冭寖鍥磋鍙栧揩鐓э紱鏈壒鍑嗗彛寰勮繑鍥?blocked銆?"""
        metric = self._metrics_by_id.get(metric_id)
        if metric is None:
            from application.common.authorization import AuthorizationDenied
            from application.common.errors import ErrorCode

            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, "Metric does not exist")
        blocked = not metric.get("production_enabled")
        if blocked:
            return {
                "results": [],
                "quality": "blocked",
                "evidence_refs": [f"metric:{metric_id}:v{metric['version']}"],
            }
        with self._engine.connect() as connection:
            rows = connection.execute(text(
                "SELECT snapshot_id, projection_version, metric_version,"
                " product_id, cohort, window_start, window_end, numerator,"
                " denominator, rate, min_sample, quality, excluded_reasons,"
                " source_watermarks, as_of FROM analytics_metric_snapshot"
                " WHERE tenant_id=:t AND metric_id=:m"
                " AND SUBSTRING_INDEX(cohort,'-',-2) >= :cs"
                " AND SUBSTRING_INDEX(cohort,'-',-2) <= :ce"
                " ORDER BY cohort, product_id,"
                " CAST(projection_version AS UNSIGNED) DESC"
            ), {
                "t": auth.tenant_id,
                "m": metric_id,
                "cs": cohort_start,
                "ce": cohort_end,
            }).mappings().all()
        seen = set()
        latest: list = []
        for row in rows:
            key = (row["metric_id"], row["product_id"], row["cohort"])
            if key in seen:
                continue
            seen.add(key)
            latest.append(row)
        eligible = sorted(
            [row for row in latest if row["quality"] == "complete"],
            key=lambda row: (row["rate"] or 0), reverse=True,
        )
        ranks = {row["snapshot_id"]: index + 1 for index, row in enumerate(eligible)}
        results = [self._metric_result_row(row, ranks, metric, active_release)
                   for row in latest]
        overall = "complete"
        for row in latest:
            quality = row["quality"]
            if quality in ("provisional", "incomplete") and overall == "complete":
                overall = quality
        if not latest:
            overall = "incomplete"
        return {
            "results": results,
            "quality": overall,
            "evidence_refs": sorted({
                f"snapshot:{row['snapshot_id']}" for row in latest
            }),
        }

    def _metric_result_row(self, row, ranks: dict, metric: dict,
                           active_release: dict | None) -> dict:
        quality_map = {
            "complete": "complete",
            "immature": "provisional",
            "insufficient_sample": "incomplete",
            "zero_denominator": "incomplete",
            "watermark_lag": "incomplete",
            "reconciliation_difference": "incomplete",
            "quarantined": "incomplete",
        }
        excluded = row["excluded_reasons"]
        if isinstance(excluded, str):
            excluded = json.loads(excluded)
        watermarks = row["source_watermarks"]
        if isinstance(watermarks, str):
            watermarks = json.loads(watermarks)
        snapshot_quality = quality_map.get(row["quality"], "incomplete")
        eligible = snapshot_quality == "complete" and row["denominator"] >= row["min_sample"]
        return {
            "metric_id": row["metric_id"],
            "dimension_values": {"product_id": row["product_id"]},
            "numerator": int(row["numerator"]),
            "denominator": int(row["denominator"]),
            "value": float(row["rate"]) if row["rate"] is not None else None,
            "quality": snapshot_quality,
            "quality_issues": list(excluded or []),
            "coverage": 1.0 if not (excluded or []) else 0.0,
            "sample_size": int(row["denominator"]),
            "as_of": row["as_of"].isoformat() + "Z",
            "watermarks": {
                source: (entry or {}).get("max_event_time") or "1970-01-01T00:00:00Z"
                for source, entry in (watermarks or {}).items()
            },
            "versions": {
                "metric_version": str(row["metric_version"]),
                "ontology_version": (active_release or {}).get("schema_version", "v2"),
                "mapping_version": str(
                    (active_release or {}).get("mapping_version", "")
                ),
                "projection_snapshot_id": row["snapshot_id"],
            },
            "evidence_refs": [f"snapshot:{row['snapshot_id']}"],
            "rank": ranks.get(row["snapshot_id"]) if eligible else None,
            "ranking_eligible": eligible,
            "excluded_reasons": list(excluded or []),
            "default_ranking_candidate": bool(metric.get("ranking_role")
                                              == "supervisor_default_candidate"),
            "definition_status": (
                "approved" if metric.get("production_enabled")
                else "pending_business_finance_approval"
            ),
        }

