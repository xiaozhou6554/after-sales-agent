"""25C 领域单元测试：时态映射规则与发布清单摘要（TC-95/96/108 语义）。"""

from datetime import date, datetime

from domain.ontology.mapping_rules import (
    MappingRecord,
    MappingViolation,
    Resolution,
    resolve_mapping,
    validate_mapping_set,
)
from domain.ontology.release_manifest import ReleaseManifest, manifest_digest


def record(
    source_id,
    target,
    *,
    tenant="tenant-north",
    system="OMS",
    version=1,
    status="active",
    valid_from="2026-01-01",
    valid_to=None,
):
    return MappingRecord(
        tenant_id=tenant,
        source_system=system,
        source_id=source_id,
        version=version,
        canonical_product_id=target,
        valid_from=date.fromisoformat(valid_from),
        valid_to=date.fromisoformat(valid_to) if valid_to else None,
        status=status,
    )


PRODUCTS = {
    "tenant-north": {"product-air-01", "product-air-02"},
    "tenant-south": {"product-air-91"},
}


class TestValidateMappingSet:
    def test_active_must_have_exactly_one_target(self):
        records = [record("SKU-100", "product-air-01")]
        assert validate_mapping_set(records, PRODUCTS) == []

    def test_active_with_two_targets_overlapping_is_violation(self):
        records = [
            record("SKU-100", "product-air-01", version=1),
            record("SKU-100", "product-air-02", version=2),
        ]
        violations = validate_mapping_set(records, PRODUCTS)
        types = {v.violation_type for v in violations}
        assert "overlapping_validity" in types

    def test_active_without_target_is_violation(self):
        records = [record("SKU-100", None)]
        violations = validate_mapping_set(records, PRODUCTS)
        assert any(v.violation_type == "active_needs_exactly_one" for v in violations)

    def test_sequential_chain_with_different_targets_ok(self):
        records = [
            record(
                "SKU-100", "product-air-01", version=1,
                valid_from="2026-01-01", valid_to="2026-06-01",
            ),
            record("SKU-100", "product-air-02", version=2, valid_from="2026-06-01"),
        ]
        assert validate_mapping_set(records, PRODUCTS) == []

    def test_ambiguous_needs_two_distinct_targets(self):
        records = [
            record("AMB-7", "product-air-01", status="ambiguous"),
            record("AMB-7", "product-air-02", status="ambiguous"),
        ]
        assert validate_mapping_set(records, PRODUCTS) == []

    def test_ambiguous_with_single_target_is_violation(self):
        records = [
            record("AMB-7", "product-air-01", status="ambiguous"),
            record("AMB-7", "product-air-01", version=2, status="ambiguous"),
        ]
        violations = validate_mapping_set(records, PRODUCTS)
        assert any(v.violation_type == "ambiguous_needs_two" for v in violations)

    def test_unmapped_must_not_have_target(self):
        records = [record("SKU-X", "product-air-01", status="unmapped")]
        violations = validate_mapping_set(records, PRODUCTS)
        assert any(v.violation_type == "unresolved_with_target" for v in violations)

    def test_cross_tenant_target_rejected(self):
        records = [record("SKU-91", "product-air-91")]
        violations = validate_mapping_set(records, PRODUCTS)
        assert any(
            v.violation_type == "cross_tenant_or_missing_target" for v in violations
        )

    def test_missing_target_rejected(self):
        records = [record("SKU-100", "product-air-99")]
        violations = validate_mapping_set(records, PRODUCTS)
        assert any(
            v.violation_type == "cross_tenant_or_missing_target" for v in violations
        )

    def test_overlapping_validity_rejected(self):
        records = [
            record("SKU-100", "product-air-01", version=1, valid_from="2026-01-01"),
            record("SKU-100", "product-air-02", version=2, valid_from="2026-06-01"),
        ]
        violations = validate_mapping_set(records, PRODUCTS)
        assert any(v.violation_type == "overlapping_validity" for v in violations)

    def test_sequential_validity_ok(self):
        records = [
            record(
                "SKU-100", "product-air-01", version=1,
                valid_from="2026-01-01", valid_to="2026-06-01",
            ),
            record("SKU-100", "product-air-02", version=2, valid_from="2026-06-01"),
        ]
        assert validate_mapping_set(records, PRODUCTS) == []

    def test_version_conflict_rejected(self):
        records = [
            record("SKU-100", "product-air-01", version=1),
            record("SKU-100", "product-air-02", version=1),
        ]
        violations = validate_mapping_set(records, PRODUCTS)
        assert any(v.violation_type == "version_conflict" for v in violations)


class TestResolveMapping:
    def test_single_active_resolves(self):
        records = [record("SKU-100", "product-air-01")]
        resolution = resolve_mapping(records, date(2026, 10, 15))
        assert resolution == Resolution("product-air-01", "active", 1)

    def test_ambiguous_does_not_resolve(self):
        records = [
            record("AMB-7", "product-air-01", status="ambiguous"),
            record("AMB-7", "product-air-02", status="ambiguous"),
        ]
        resolution = resolve_mapping(records, date(2026, 10, 15))
        assert resolution.canonical_product_id is None
        assert resolution.status == "ambiguous"
        assert resolution.mapping_version is None

    def test_unmapped_when_no_rows(self):
        resolution = resolve_mapping([], date(2026, 10, 15))
        assert resolution.status == "unmapped"

    def test_lapsed_interval_reports_unmapped(self):
        records = [
            record(
                "SKU-100", "product-air-01", status="expired",
                valid_from="2026-01-01", valid_to="2026-02-01",
            )
        ]
        resolution = resolve_mapping(records, date(2026, 10, 15))
        assert resolution.status == "unmapped"
        assert resolution.canonical_product_id is None

    def test_expired_status_covering_reports_expired(self):
        records = [
            record(
                "SKU-100", "product-air-01", status="expired",
                valid_from="2026-01-01",
            )
        ]
        resolution = resolve_mapping(records, date(2026, 10, 15))
        assert resolution.status == "expired"
        assert resolution.canonical_product_id is None

    def test_versioned_rows_pick_covering_one(self):
        records = [
            record(
                "SKU-100", "product-air-01", version=1,
                valid_from="2026-01-01", valid_to="2026-06-01",
            ),
            record("SKU-100", "product-air-02", version=2, valid_from="2026-06-01"),
        ]
        assert resolve_mapping(
            records, date(2026, 5, 1)
        ).canonical_product_id == "product-air-01"
        after = resolve_mapping(records, date(2026, 8, 1))
        assert after.canonical_product_id == "product-air-02"
        assert after.mapping_version == 2


class TestReleaseManifest:
    def test_digest_is_deterministic(self):
        manifest = {
            "release_id": "rel-1",
            "tenant_id": "tenant-north",
            "schema_version": "v2",
            "mapping_version": 2,
            "metric_version": 1,
            "files": {"core.ttl": "a", "metrics.json": "b"},
            "effective_at": None,
        }
        assert manifest_digest(manifest) == manifest_digest(dict(reversed(list(manifest.items()))))
        assert manifest_digest(manifest) != manifest_digest(
            {**manifest, "mapping_version": 3}
        )

    def test_dataclass_verify_digest(self):
        dataclass_manifest = ReleaseManifest(
            release_id="rel-1",
            tenant_id="tenant-north",
            schema_version="v2",
            mapping_version=2,
            metric_version=1,
            files={"core.ttl": "a"},
            effective_at=datetime(2026, 10, 1, 8, 0, 0),
        )
        payload = dataclass_manifest.to_dict()
        assert dataclass_manifest.verify_digest(manifest_digest(payload))
        assert not dataclass_manifest.verify_digest("0" * 64)

    def test_extra_fields_change_digest(self):
        dataclass_manifest = ReleaseManifest(
            release_id="rel-1",
            tenant_id="tenant-north",
            schema_version="v2",
            mapping_version=2,
            metric_version=1,
            files={"core.ttl": "a"},
            effective_at=None,
        )
        with_extra = ReleaseManifest(
            release_id="rel-1",
            tenant_id="tenant-north",
            schema_version="v2",
            mapping_version=2,
            metric_version=1,
            files={"core.ttl": "a"},
            effective_at=None,
            extra={"compat": "breaking"},
        )
        assert dataclass_manifest.digest() != with_extra.digest()


def test_mapping_violation_carries_context():
    violation = MappingViolation(
        violation_type="cross_tenant_or_missing_target",
        tenant_id="tenant-north",
        source_system="OMS",
        source_id="SKU-91",
        detail="target product-air-91 not in tenant",
    )
    assert violation.violation_type == "cross_tenant_or_missing_target"
    assert violation.source_id == "SKU-91"


def test_resolution_defaults():
    resolution = Resolution(
        canonical_product_id=None, status="unmapped", mapping_version=None
    )
    assert resolution.mapping_version is None
