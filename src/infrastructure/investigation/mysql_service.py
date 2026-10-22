"""MySQL-backed supervisor investigation and anomaly query adapter."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Engine

from application.analytics.projection_service import AnalyticsProjectionService
from application.common.authorization import AuthorizationDenied
from application.common.errors import ErrorCode
from domain.investigation.contribution import (
    FactorGroup,
    comparability_gate,
    factor_contributions,
    percentage_points,
)

ENVIRONMENT = "simulation"

_RETURN_METRICS = {"return_passed_item_rate"}


class MySQLInvestigationAdapter:
    def __init__(
        self,
        engine: Engine,
        projection: AnalyticsProjectionService,
    ) -> None:
        self._engine = engine
        self._projection = projection

    # ---------- 鎸囨爣寮傚父璋冩煡锛圱C-110/111/112/113/114/115/116/123锛?----------

    def open_metric_anomaly(
        self,
        auth,
        *,
        metric_id: str,
        product_id: str,
        cohort: str,
        baseline_cohort: str,
        threshold_pp: str = "5",
        actor_subject_id: str | None = None,
        reason: str | None = None,
    ) -> dict:
        computed = self._compute_metric_anomaly(
            auth,
            metric_id=metric_id,
            product_id=product_id,
            cohort=cohort,
            baseline_cohort=baseline_cohort,
            threshold_pp=threshold_pp,
        )
        return self._persist_revision(
            auth,
            anomaly_type="business_metric",
            subject=f"{metric_id}:{product_id}:{cohort}",
            basis_snapshot_version=computed["basis_snapshot_version"],
            basis_as_of=computed["basis_as_of"],
            cause_status=computed["cause_status"],
            gate=computed["gate"],
            findings=computed["findings"],
            metric_version=computed["metric_version"],
            ontology_version="v2",
            mapping_version=self._active_mapping_version(auth.tenant_id),
            snapshot_id=computed["snapshot_id"],
            actor_subject_id=actor_subject_id or auth.subject_id,
            reason=reason,
        )

    def _compute_metric_anomaly(
        self, auth, *, metric_id: str, product_id: str, cohort: str,
        baseline_cohort: str, threshold_pp: str = "5",
    ) -> dict:
        """鎸夋渶鏂板揩鐓у绠楀紓甯镐笌璐＄尞锛涜川閲忛棬涓嶅悎鏍煎仠姝㈡帓鍚嶏紙TC-113/114锛夈€?"""
        minimum_sample, metric_version = self._metric_meta(metric_id)
        now_row = self._latest_snapshot(auth.tenant_id, metric_id, product_id,
                                        cohort)
        base_row = self._latest_snapshot(auth.tenant_id, metric_id, product_id,
                                         baseline_cohort)
        comparable, issues = comparability_gate(
            now_quality=now_row and now_row["quality"],
            base_quality=base_row and base_row["quality"],
            now_denominator=now_row and int(now_row["denominator"]),
            base_denominator=base_row and int(base_row["denominator"]),
            minimum_sample=minimum_sample,
        )
        delta_pp = None
        if now_row and base_row:
            delta_pp = percentage_points(
                _to_decimal(now_row["rate"]), _to_decimal(base_row["rate"])
            )
        gate = {
            "comparable": comparable,
            "issues": issues,
            "delta_pp": float(delta_pp) if delta_pp is not None else None,
            "threshold_pp": float(threshold_pp),
            "minimum_sample": minimum_sample,
            "baseline_cohort": baseline_cohort,
        }
        if comparable and now_row and base_row:
            basis_version = max(
                int(now_row["projection_version"]),
                int(base_row["projection_version"]),
            )
            now_groups = self._factor_groups(
                auth.tenant_id, metric_id, product_id, cohort, basis_version
            )
            base_groups = self._factor_groups(
                auth.tenant_id, metric_id, product_id, baseline_cohort,
                basis_version,
            )
            contributions = factor_contributions(now_groups, base_groups)
            findings = [
                {
                    "cause_code": item.cause_code,
                    "explanation": (
                        f"鍒嗙粍澶嶇畻锛氬綋鍓?{item.numerator_now}/"
                        f"{item.denominator_now}锛屽熀鍑?{item.numerator_base}/"
                        f"{item.denominator_base}锛涜础鐚寜缁勭巼宸椾唤棰濊绠楋紝"
                        " \u76f8\u5173\u6027\u8bc1\u636e\u4ec5\u7528\u4e8e\u8bf4\u660e\u8d21\u732e?"
                        "\u4e0d\u80fd\u5355\u72ec\u786e\u8ba4\u56e0\u679c\u3002"
                    ),
                    "contribution": (
                        float(item.contribution_pp)
                        if item.contribution_pp is not None else None
                    ),
                    "evidence_refs": [
                        f"snapshot:{now_row['snapshot_id']}",
                        f"snapshot:{base_row['snapshot_id']}",
                        f"metric:{metric_id}:v{metric_version}",
                    ],
                }
                for item in contributions
            ]
            cause_status = (
                "supported"
                if delta_pp is not None
                and abs(delta_pp) >= Decimal(threshold_pp)
                else "inconclusive"
            )
            return {
                "basis_snapshot_version": str(basis_version),
                "basis_as_of": date.today(),
                "cause_status": cause_status,
                "gate": gate,
                "findings": findings,
                "metric_version": str(metric_version),
                "snapshot_id": str(now_row["snapshot_id"]),
            }
        # Compare snapshot versions before producing the investigation result.
        basis_version = int(
            (now_row or base_row)["projection_version"]
        ) if (now_row or base_row) else 0
        snapshot_id = (now_row or base_row or {}).get(
            "snapshot_id", f"none:{metric_id}:{product_id}:{cohort}"
        )
        return {
            "basis_snapshot_version": str(basis_version),
            "basis_as_of": date.today(),
            "cause_status": "data_quality_blocked",
            "gate": gate,
            "findings": [],
            "metric_version": str(metric_version),
            "snapshot_id": str(snapshot_id)[:64],
        }

    def explain(self, auth, investigation_id: str, *, revision: int | None
                ) -> dict:
        """鎸夋巿鏉冭繑鍥炰笉鍙彉璋冩煡淇锛圡CP explain_anomaly 鏁版嵁闈級銆?"""
        record = self.get_revision(auth.tenant_id, investigation_id, revision)
        if record is None:
            raise AuthorizationDenied(
                ErrorCode.RESOURCE_UNAVAILABLE,
                "Resource is missing or outside the authorized scope",
            )
        return {
            "investigation_id": record["investigation_id"],
            "revision": int(record["revision"]),
            "anomaly_type": record["anomaly_type"],
            "cause_status": record["cause_status"],
            "findings": record["findings"],
            "quality_issues": record["gate"].get("issues", []),
            "metric_version": record["metric_version"],
            "ontology_version": record["ontology_version"],
            "mapping_version": record["mapping_version"],
            "snapshot_id": record["snapshot_id"],
            "evidence_refs": sorted({
                ref for finding in record["findings"]
                for ref in finding["evidence_refs"]
            }) or [f"investigation:{investigation_id}:r{record['revision']}"],
        }

    def replay(self, auth, investigation_id: str, revision: int) -> dict:
        """鍥哄畾蹇収鐗堟湰閲嶆斁锛氫粠 basis 浜嬪疄澶嶇畻璐＄尞骞朵笌鏃㈡湁淇姣斿銆?"""
        record = self.get_revision(auth.tenant_id, investigation_id, revision)
        if record is None:
            raise AuthorizationDenied(
                ErrorCode.RESOURCE_UNAVAILABLE, "璧勬簮涓嶅瓨鍦ㄦ垨涓嶅湪鎺堟潈鑼冨洿鍐?"
            )
        metric_id, product_id, cohort = record["subject"].split(":", 2)
        minimum_sample, _ = self._metric_meta(metric_id)
        now_row = self._snapshot_at(
            auth.tenant_id, metric_id, product_id, cohort,
            record["basis_snapshot_version"],
        )
        baseline_cohort = record["gate"].get("baseline_cohort")
        base_row = (
            self._snapshot_at(
                auth.tenant_id, metric_id, product_id, baseline_cohort,
                record["basis_snapshot_version"],
            )
            if baseline_cohort else None
        )
        recomputed: list[dict] = []
        if now_row and base_row:
            now_groups = self._factor_groups(
                auth.tenant_id, metric_id, product_id, cohort,
                int(record["basis_snapshot_version"]),
            )
            base_groups = self._factor_groups(
                auth.tenant_id, metric_id, product_id, baseline_cohort,
                int(record["basis_snapshot_version"]),
            )
            recomputed = [
                {
                    "cause_code": item.cause_code,
                    "contribution": (
                        float(item.contribution_pp)
                        if item.contribution_pp is not None else None
                    ),
                }
                for item in factor_contributions(now_groups, base_groups)
            ]
        stored = [
            {"cause_code": finding["cause_code"],
             "contribution": finding["contribution"]}
            for finding in record["findings"]
        ]
        return {
            "investigation_id": investigation_id,
            "revision": revision,
            "basis_snapshot_version": record["basis_snapshot_version"],
            "matches": recomputed == stored,
            "conclusion_status": record["cause_status"],
        }

    def revise(
        self, auth, investigation_id: str, *, actor_subject_id: str,
        reason: str,
    ) -> dict:
        """淇锛氭寜褰撳墠鏈€鏂板揩鐓ч噸鏂拌绠楋紝鏂颁慨璁㈠彧澧炰笉鏀癸紙TC-116/123锛夈€?"""
        head = self.get_revision(auth.tenant_id, investigation_id, None)
        if head is None:
            raise AuthorizationDenied(
                ErrorCode.RESOURCE_UNAVAILABLE, "璧勬簮涓嶅瓨鍦ㄦ垨涓嶅湪鎺堟潈鑼冨洿鍐?"
            )
        metric_id, product_id, cohort = head["subject"].split(":", 2)
        baseline_cohort = head["gate"].get("baseline_cohort") or cohort
        fresh = self._compute_metric_anomaly(
            auth,
            metric_id=metric_id,
            product_id=product_id,
            cohort=cohort,
            baseline_cohort=baseline_cohort,
            threshold_pp=str(head["gate"].get("threshold_pp", "5")),
        )
        next_revision = int(head["revision"]) + 1
        with self._engine.begin() as connection:
            connection.execute(text(
                "UPDATE investigation SET current_revision=:rev,"
                " updated_at=:now WHERE tenant_id=:t AND investigation_id=:i"
                " AND current_revision=:expected"
            ), {"rev": next_revision, "now": self._now(), "t": auth.tenant_id,
                "i": investigation_id, "expected": head["revision"]})
        return self._persist_revision(
            auth,
            anomaly_type="business_metric",
            subject=head["subject"],
            investigation_id=investigation_id,
            basis_snapshot_version=fresh["basis_snapshot_version"],
            basis_as_of=fresh["basis_as_of"],
            cause_status=fresh["cause_status"],
            gate=fresh["gate"],
            findings=fresh["findings"],
            metric_version=fresh["metric_version"],
            ontology_version="v2",
            mapping_version=self._active_mapping_version(auth.tenant_id),
            snapshot_id=fresh["snapshot_id"],
            actor_subject_id=actor_subject_id,
            reason=reason,
            revision=next_revision,
        )

    def get_revision(
        self, tenant_id: str, investigation_id: str, revision: int | None
    ) -> dict | None:
        with self._engine.connect() as connection:
            head = connection.execute(text(
                "SELECT investigation_id,anomaly_type,subject,status,"
                " current_revision FROM investigation"
                " WHERE tenant_id=:t AND investigation_id=:i"
            ), {"t": tenant_id, "i": investigation_id}).mappings().first()
            if head is None:
                return None
            target = revision or int(head["current_revision"])
            row = connection.execute(text(
                "SELECT r.investigation_id,r.revision,r.basis_snapshot_version,"
                " r.basis_as_of,r.cause_status,r.gate_json,r.findings_json,"
                " r.metric_version,r.ontology_version,r.mapping_version,"
                " r.snapshot_id,r.reason,r.created_by,r.created_at,"
                " i.anomaly_type,i.subject,i.status"
                " FROM investigation_revision r"
                " JOIN investigation i ON i.tenant_id=r.tenant_id"
                " AND i.investigation_id=r.investigation_id"
                " WHERE r.tenant_id=:t AND r.investigation_id=:i"
                " AND r.revision=:rev"
            ), {"t": tenant_id, "i": investigation_id, "rev": target}
            ).mappings().first()
        if row is None:
            return None
        record = dict(row)
        record["gate"] = _loads(record["gate_json"])
        record["findings"] = _loads(record["findings_json"])
        record["basis"] = {
            "basis_snapshot_version": record["basis_snapshot_version"],
            "basis_as_of": str(record["basis_as_of"]),
        }
        return record

    def list_investigations(self, tenant_id: str) -> list[dict]:
        with self._engine.connect() as connection:
            rows = connection.execute(text(
                "SELECT investigation_id,anomaly_type,subject,status,"
                " current_revision,created_by,created_at,updated_at"
                " FROM investigation WHERE tenant_id=:t ORDER BY created_at"
            ), {"t": tenant_id}).mappings().all()
        return [dict(row) for row in rows]

    # ---------- 杩愯涓庤法绯荤粺寮傚父鍙戠幇锛圱C-117~121锛屽彧璇昏瘉鎹潰锛?----------

    def detect_runtime_anomalies(self, auth) -> dict:
        tenant_id = auth.tenant_id
        with self._engine.connect() as connection:
            tool_errors = connection.execute(text(
                "SELECT tool_name,status,error,COUNT(*) AS cnt,"
                " MIN(created_at) AS first_at FROM tool_invocation"
                " WHERE tenant_id=:t AND (error IS NOT NULL OR status IN"
                " ('failed','error','timeout'))"
                " GROUP BY tool_name,status,error ORDER BY first_at"
            ), {"t": tenant_id}).mappings().all()
            outbox = connection.execute(text(
                "SELECT status,COUNT(*) AS cnt FROM outbox_event"
                " WHERE tenant_id=:t AND status != 'published'"
                " GROUP BY status"
            ), {"t": tenant_id}).all()
            inbox_fail = connection.execute(text(
                "SELECT COUNT(*) FROM consumer_inbox WHERE tenant_id=:t"
                " AND status='failed'"
            ), {"t": tenant_id}).scalar()
            inbox_dup = connection.execute(text(
                "SELECT COUNT(*) FROM (SELECT idempotency_key FROM"
                " consumer_inbox WHERE tenant_id=:t"
                " GROUP BY idempotency_key HAVING COUNT(*) > 1) d"
            ), {"t": tenant_id}).scalar()
            event_count = connection.execute(text(
                "SELECT COUNT(*) FROM outbox_event WHERE tenant_id=:t"
            ), {"t": tenant_id}).scalar()
            trace_count = connection.execute(text(
                "SELECT COUNT(*) FROM mock_evt_trace WHERE tenant_id=:t"
            ), {"t": tenant_id}).scalar()
        failures = []
        for row in tool_errors:
            error_text = str(row["error"] or row["status"])
            failures.append({
                "dependency": row["tool_name"],
                "status": row["status"],
                "occurrences": int(row["cnt"]),
                "first_failure_at": str(row["first_at"]),
                "classification": (
                    "timeout" if "timeout" in error_text
                    else "business_rejection" if any(
                        marker in error_text
                        for marker in ("denied", "conflict", "rejected")
                    ) else "error"
                ),
            })
        backlog = {status: int(cnt) for status, cnt in outbox}
        coverage_ratio = (
            round(int(trace_count) / int(event_count), 4)
            if int(event_count) > 0 else None
        )
        quality_issues = []
        if coverage_ratio is None or coverage_ratio < 1.0:
            quality_issues.append("telemetry_coverage_gap")
        return {
            "results": {
                "dependency_failures": failures,
                "outbox_backlog": backlog,
                "inbox_failed": int(inbox_fail or 0),
                "inbox_duplicate_keys": int(inbox_dup or 0),
                "trace_coverage_ratio": coverage_ratio,
            },
            "quality_issues": quality_issues,
            "statement": (
                "Runtime records show observed failures and coverage gaps; "
                "they do not establish or invent a root cause. \u4e0d\u7f16\u9020\u6839\u56e0"
            ),
        }

    def detect_cross_system_conflicts(self, auth) -> dict:
        tenant_id = auth.tenant_id
        with self._engine.connect() as connection:
            differences = connection.execute(text(
                "SELECT difference_id,difference_type,execution_id,amount_minor"
                " FROM reconciliation_difference WHERE tenant_id=:t"
                " ORDER BY created_at LIMIT 50"
            ), {"t": tenant_id}).mappings().all()
            ambiguous = connection.execute(text(
                "SELECT source_system,source_id,version,canonical_product_id"
                " FROM mock_pim_product_mapping WHERE tenant_id=:t"
                " AND status IN ('ambiguous','unmapped') LIMIT 50"
            ), {"t": tenant_id}).all()
        return {
            "results": {
                "reconciliation_differences": [
                    {
                        "difference_id": row["difference_id"],
                        "difference_type": row["difference_type"],
                        "execution_id": row["execution_id"],
                        "amount_minor": int(row["amount_minor"]),
                    }
                    for row in differences
                ],
                "non_active_mappings": [
                    {
                        "source_system": row[0],
                        "source_id": row[1],
                        "version": int(row[2]),
                        "status_reference": row[3],
                    }
                    for row in ambiguous
                ],
            },
            "owners": {
                "orders": "OMS", "payments": "鏀粯娓犻亾", "receipts": "WMS",
                "mappings": "PIM",
            },
            "statement": (
                "Cross-system records identify ownership and evidence; "
                "reconciliation differences do not trigger automatic reversal. "
                "\u4e0d\u81ea\u52a8\u51b2\u9500"
            ),
        }

    def detect_knowledge_anomalies(self, auth) -> dict:
        with self._engine.connect() as connection:
            jobs = connection.execute(text(
                "SELECT job_id,knowledge_release_id,backend,status,attempts,"
                " error FROM knowledge_index_job WHERE tenant_id=:t"
                " AND status NOT IN ('succeeded','completed')"
                " ORDER BY job_id LIMIT 50"
            ), {"t": self._tenant_of(auth)}).mappings().all()
            releases = connection.execute(text(
                "SELECT knowledge_release_id,version,status,is_active,"
                " failure_reason FROM knowledge_release WHERE tenant_id=:t"
                " AND status != 'active' ORDER BY version LIMIT 20"
            ), {"t": self._tenant_of(auth)}).mappings().all()
        return {
            "results": {
                "failed_index_jobs": [dict(row) for row in jobs],
                "inactive_releases": [dict(row) for row in releases],
            },
            "statement": (
                "Failed indexing jobs and inactive release manifests are evidence; "
                "they must not be described as model root causes. "
                "\u4e0d\u5f97\u8868\u8ff0\u4e3a\u6a21\u578b\u6839\u56e0"
            ),
        }

    # ---------- 鍐呴儴 ----------

    def _persist_revision(
        self, auth, *, anomaly_type: str, subject: str,
        basis_snapshot_version: str, basis_as_of: date, cause_status: str,
        gate: dict, findings: list[dict], metric_version: str | None,
        ontology_version: str | None, mapping_version: str | None,
        snapshot_id: str, actor_subject_id: str, reason: str | None,
        investigation_id: str | None = None, revision: int | None = None,
    ) -> dict:
        now = self._now()
        with self._engine.begin() as connection:
            if investigation_id is None:
                seed = "|".join((
                    auth.tenant_id, subject, basis_snapshot_version,
                    uuid.uuid4().hex,
                ))
                investigation_id = f"INV-{uuid.uuid5(uuid.NAMESPACE_URL, seed).hex[:24]}"
                connection.execute(text(
                    "INSERT INTO investigation (tenant_id,investigation_id,"
                    " anomaly_type,subject,status,current_revision,environment,"
                    " created_by,created_at,updated_at) VALUES (:t,:i,:atype,"
                    " :subject,'open',1,:env,:by,:now,:now)"
                ), {"t": auth.tenant_id, "i": investigation_id,
                    "atype": anomaly_type, "subject": subject[:255],
                    "env": ENVIRONMENT, "by": actor_subject_id, "now": now})
                target_revision = 1
            else:
                target_revision = revision or 1
            connection.execute(text(
                "INSERT INTO investigation_revision (tenant_id,"
                " investigation_id,revision,basis_snapshot_version,basis_as_of,"
                " cause_status,gate_json,findings_json,metric_version,"
                " ontology_version,mapping_version,snapshot_id,reason,"
                " created_by,created_at) VALUES (:t,:i,:rev,:basis,:asof,"
                " :cause,:gate,:findings,:mv,:ov,:mapv,:sid,:reason,:by,:now)"
            ), {"t": auth.tenant_id, "i": investigation_id,
                "rev": target_revision, "basis": basis_snapshot_version,
                "asof": basis_as_of, "cause": cause_status,
                "gate": _dumps(gate), "findings": _dumps(findings),
                "mv": metric_version, "ov": ontology_version,
                "mapv": mapping_version, "sid": snapshot_id,
                "reason": reason, "by": actor_subject_id, "now": now})
        return {
            "investigation_id": investigation_id,
            "revision": target_revision,
            "anomaly_type": anomaly_type,
            "cause_status": cause_status,
            "findings": findings,
            "gate": gate,
            "metric_version": metric_version,
            "ontology_version": ontology_version,
            "mapping_version": mapping_version,
            "snapshot_id": snapshot_id,
            "basis": {
                "basis_snapshot_version": basis_snapshot_version,
                "basis_as_of": basis_as_of.isoformat(),
            },
        }

    def _latest_snapshot(self, tenant_id: str, metric_id: str, product_id: str,
                         cohort: str) -> dict | None:
        with self._engine.connect() as connection:
            row = connection.execute(text(
                "SELECT snapshot_id,projection_version,metric_version,numerator,"
                " denominator,rate,quality FROM analytics_metric_snapshot"
                " WHERE tenant_id=:t AND metric_id=:m AND product_id=:p"
                " AND cohort=:c ORDER BY CAST(projection_version AS UNSIGNED)"
                " DESC LIMIT 1"
            ), {"t": tenant_id, "m": metric_id, "p": product_id, "c": cohort}
            ).mappings().first()
        return dict(row) if row else None

    def _snapshot_at(self, tenant_id: str, metric_id: str, product_id: str,
                     cohort: str, version: str) -> dict | None:
        with self._engine.connect() as connection:
            row = connection.execute(text(
                "SELECT snapshot_id,projection_version,metric_version,numerator,"
                " denominator,rate,quality FROM analytics_metric_snapshot"
                " WHERE tenant_id=:t AND metric_id=:m AND product_id=:p"
                " AND cohort=:c AND projection_version=:v LIMIT 1"
            ), {"t": tenant_id, "m": metric_id, "p": product_id, "c": cohort,
                "v": version}).mappings().first()
        return dict(row) if row else None

    def _factor_groups(
        self, tenant_id: str, metric_id: str, product_id: str, cohort: str,
        version: int,
    ) -> list[FactorGroup]:
        """浠庡浐瀹氱増鏈簨瀹炴寜缁勫绠楀垎瀛?鍒嗘瘝锛堟壒娆℃垨璁㈠崟绮掑害锛夈€?"""
        month = cohort.split("-", 1)[1] if "-" in cohort else cohort
        if metric_id in _RETURN_METRICS:
            num_type, den_type = "wms_inspection", "shipment_batch"
        else:
            num_type, den_type = "refund_line", "payment_line"
        with self._engine.connect() as connection:
            numerator = connection.execute(text(
                "SELECT source_record_id,quantity,goods_minor,payload"
                " FROM analytics_fact WHERE tenant_id=:t"
                " AND projection_version=:v AND fact_type=:ft"
                " AND canonical_product_id=:p AND SUBSTRING(cohort_id,1,7)=:m"
                " AND mapping_status='active'"
            ), {"t": tenant_id, "v": str(version), "ft": num_type,
                "p": product_id, "m": month}).all()
            denominator = connection.execute(text(
                "SELECT source_record_id,quantity,goods_minor,payload"
                " FROM analytics_fact WHERE tenant_id=:t"
                " AND projection_version=:v AND fact_type=:ft"
                " AND canonical_product_id=:p AND SUBSTRING(cohort_id,1,7)=:m"
                " AND mapping_status='active'"
            ), {"t": tenant_id, "v": str(version), "ft": den_type,
                "p": product_id, "m": month}).all()
        def group_key(record) -> str:
            if metric_id in _RETURN_METRICS:
                return f"batch:{record[0]}"
            payload = _loads(record[3]) or {}
            return f"order:{payload.get('order_id') or record[0]}"
        amounts = metric_id == "refund_goods_amount_rate"
        buckets: dict[str, list[int]] = {}
        for record in numerator:
            key = group_key(record)
            value = int(record[2] or 0) if amounts else int(record[1] or 0)
            buckets.setdefault(key, [0, 0])
            buckets[key][0] += value
        groups: list[FactorGroup] = []
        for record in denominator:
            key = group_key(record)
            value = int(record[2] or 0) if amounts else int(record[1] or 0)
            buckets.setdefault(key, [0, 0])
            buckets[key][1] += value
        for key, (num, den) in buckets.items():
            groups.append(FactorGroup(key, num, den))
        return groups

    def _metric_meta(self, metric_id: str) -> tuple[int, int]:
        metrics = self._projection.metric_definitions
        metric = metrics.get(metric_id)
        if metric is None:
            raise AuthorizationDenied(
                ErrorCode.RESOURCE_UNAVAILABLE, "鎸囨爣涓嶅瓨鍦?"
            )
        return int(metric["minimum_sample"]), int(metric["version"])

    def _active_mapping_version(self, tenant_id: str) -> str | None:
        with self._engine.connect() as connection:
            row = connection.execute(text(
                "SELECT mapping_version FROM ontology_release"
                " WHERE tenant_id=:t AND status='active' LIMIT 1"
            ), {"t": tenant_id}).first()
        return str(int(row[0])) if row and row[0] is not None else None

    def _tenant_of(self, auth) -> str:
        return auth.tenant_id

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc).replace(tzinfo=None)


def _to_decimal(value) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _loads(value):
    import json

    if isinstance(value, str):
        return json.loads(value)
    return value


def _dumps(value) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, default=str)

