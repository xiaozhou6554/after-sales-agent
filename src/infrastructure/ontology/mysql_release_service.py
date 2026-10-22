"""鏈綋鍙戝竷缂栨帓锛氳崏绋库啋SHACL 鏍￠獙鈫抮evision 鍘熷瓙婵€娲?鍥炴粴锛堣鏍?28.2锛夈€?
浠讳竴姝ュけ璐ヤ笉鏀瑰姩褰撳墠婵€娲绘寚閽堬紱鍙戝竷娓呭崟鍐荤粨鏂囦欢鎽樿涓庢槧灏?鎸囨爣鐗堟湰銆?"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine

from domain.ontology.release_manifest import ReleaseManifest
from infrastructure.ontology.mysql_identity_mapping import MySqlIdentityMapping
from infrastructure.ontology.mysql_release_repository import (
    MySqlOntologyReleaseRepository,
    ReleaseConflict,
)
from infrastructure.ontology.shacl_validator import ONTOLOGY_DIR, OntologyValidator

METRIC_APPROVALS_PATH = (
    Path(__file__).resolve().parents[3]
    / "data" / "contracts" / "metric-approval-register.json"
)


class MySQLOntologyReleaseAdapter:
    def __init__(
        self,
        engine: Engine,
        repository: MySqlOntologyReleaseRepository,
        identity_mapping: MySqlIdentityMapping,
        validator: OntologyValidator | None = None,
    ) -> None:
        self._engine = engine
        self._repo = repository
        self._mapping = identity_mapping
        self._validator = validator or OntologyValidator()

    def draft(
        self,
        tenant_id: str,
        release_id: str,
        *,
        effective_at: datetime | None = None,
    ) -> dict:
        self._assert_mapping_consistent(tenant_id)
        metrics = self._metrics()
        mapping_version = self._max_mapping_version(tenant_id)
        metric_version = max(int(row["version"]) for row in metrics)
        manifest = ReleaseManifest(
            release_id=release_id,
            tenant_id=tenant_id,
            schema_version="v2",
            mapping_version=mapping_version,
            metric_version=metric_version,
            files=self._file_digests(),
            effective_at=effective_at,
        ).to_dict()
        digest = self._validator_manifest_digest()
        self._repo.upsert_candidate(
            tenant_id,
            release_id,
            schema_version="v2",
            mapping_version=mapping_version,
            metric_version=metric_version,
            manifest=manifest,
            digest=digest,
            effective_at=effective_at,
        )
        return self._repo.get(tenant_id, release_id)

    def validate_release(self, tenant_id: str, release_id: str) -> dict:
        release = self._repo.get(tenant_id, release_id)
        if release is None:
            raise ReleaseConflict("Ontology release draft does not exist")
        products = self._products(tenant_id)
        mappings = self._mapping_record_dicts(tenant_id)
        metrics = self._metrics()
        approvals = json.loads(METRIC_APPROVALS_PATH.read_text(encoding="utf-8"))
        result = self._validator.validate_candidate(
            mappings, products, metrics, approvals, release
        )
        if not result.conforms:
            raise ReleaseConflict(
                "SHACL 鏍￠獙鏈€氳繃锛屼繚鐣欏綋鍓嶆縺娲绘寚閽堜笉鍙橈細"
                + result.report_text[:2000]
            )
        self._repo.mark_validated(tenant_id, release_id)
        return {
            "release_id": release_id,
            "conforms": True,
            "manifest_digest": result.manifest_digest,
            "report_digest_excerpt": result.report_text[:500],
        }

    def publish(
        self,
        tenant_id: str,
        release_id: str,
        *,
        expected_active_revision: int | None,
        actor_subject_id: str,
        trace=None,
    ) -> dict:
        return self._repo.activate(
            tenant_id,
            release_id,
            expected_active_revision=expected_active_revision,
            actor_subject_id=actor_subject_id,
            trace=trace,
        )

    def rollback(
        self,
        tenant_id: str,
        target_release_id: str,
        *,
        expected_active_revision: int | None,
        actor_subject_id: str,
        trace=None,
    ) -> dict:
        target = self._repo.get(tenant_id, target_release_id)
        if target is None or target["status"] not in ("validated", "retired", "active"):
            raise ReleaseConflict("Rollback target is missing or unavailable")
        return self._repo.activate(
            tenant_id,
            target_release_id,
            expected_active_revision=expected_active_revision,
            actor_subject_id=actor_subject_id,
            trace=trace,
        )

    def active(self, tenant_id: str) -> dict | None:
        return self._repo.get_active(tenant_id)

    def get(self, tenant_id: str, release_id: str) -> dict | None:
        return self._repo.get(tenant_id, release_id)

    def history(self, tenant_id: str) -> list[dict]:
        return self._repo.list_validated(tenant_id)

    def _assert_mapping_consistent(self, tenant_id: str) -> None:
        violations = self._mapping.validate_tenant(tenant_id)
        if violations:
            details = "; ".join(
                f"{v.violation_type}:{v.source_system}/{v.source_id}"
                for v in violations[:10]
            )
            raise ReleaseConflict(
                "Mapping set has structural violations; draft rejected: " + details
            )

    def _products(self, tenant_id: str) -> list[dict]:
        with self._engine.connect() as connection:
            rows = connection.execute(text(
                "SELECT tenant_id,canonical_product_id FROM mock_pim_product"
                " WHERE tenant_id=:t"
            ), {"t": tenant_id}).mappings().all()
        return [dict(row) for row in rows]

    def _mapping_record_dicts(self, tenant_id: str) -> list[dict]:
        records = self._mapping.load_records(tenant_id)
        return [
            {
                "tenant_id": record.tenant_id,
                "source_system": record.source_system,
                "source_id": record.source_id,
                "version": record.version,
                "canonical_product_id": record.canonical_product_id,
                "valid_from": (
                    record.valid_from.isoformat()
                    if isinstance(record.valid_from, date)
                    else str(record.valid_from)
                ),
                "valid_to": (
                    record.valid_to.isoformat()
                    if record.valid_to is not None
                    and isinstance(record.valid_to, date)
                    else record.valid_to
                ),
                "status": record.status,
            }
            for record in records
        ]

    def _metrics(self) -> list[dict]:
        path = ONTOLOGY_DIR / "metrics.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def _max_mapping_version(self, tenant_id: str) -> int:
        with self._engine.connect() as connection:
            value = connection.execute(text(
                "SELECT COALESCE(MAX(version),0) FROM mock_pim_product_mapping"
                " WHERE tenant_id=:t"
            ), {"t": tenant_id}).scalar()
        return int(value or 0)

    def _file_digests(self) -> dict[str, str]:
        import hashlib

        digests = {}
        for name in ("core.ttl", "constraints.shacl.ttl", "source_mappings.json",
                     "metrics.json"):
            digests[name] = hashlib.sha256(
                (ONTOLOGY_DIR / name).read_bytes()
            ).hexdigest()
        return digests

    def _validator_manifest_digest(self) -> str:
        import hashlib

        digest = hashlib.sha256()
        for name in ("core.ttl", "constraints.shacl.ttl", "source_mappings.json",
                     "metrics.json"):
            digest.update((ONTOLOGY_DIR / name).read_bytes())
        return digest.hexdigest()

