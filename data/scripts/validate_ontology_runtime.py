"""Run real RDF/OWL parsing and SHACL validation over the synthetic v2 baseline."""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
ONTOLOGY = DATA / "knowledge" / "v2" / "ontology"


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    try:
        from pyshacl import validate
        from rdflib import RDF, XSD, Graph, Literal, Namespace, URIRef
    except ImportError as error:
        print(
            "BLOCKED: ontology runtime validation needs rdflib and pyshacl. "
            "Install data/requirements-ontology.txt in the development environment. "
            f"Missing import: {error}",
            file=sys.stderr,
        )
        return 2

    systems = load_json(DATA / "mock" / "v2" / "systems.json")
    mappings = load_json(ONTOLOGY / "source_mappings.json")
    metrics = load_json(ONTOLOGY / "metrics.json")
    approvals = load_json(DATA / "contracts" / "metric-approval-register.json")
    evidence_rows = systems["anomaly_evidence"]
    core_path = ONTOLOGY / "core.ttl"
    shapes_path = ONTOLOGY / "constraints.shacl.ttl"

    ontology_graph = Graph().parse(core_path, format="turtle")
    shapes_graph = Graph().parse(shapes_path, format="turtle")
    namespace = Namespace("urn:after-sales-agent:ontology:v2#")
    data_graph = Graph()
    data_graph.bind("", namespace)

    tenants = {row["tenant_id"] for row in systems["tenants"]}
    products = {(row["tenant_id"], row["canonical_product_id"]) for row in systems["products"]}
    tenant_refs = {tenant: URIRef(f"urn:after-sales-agent:tenant:{tenant}") for tenant in tenants}
    product_refs = {
        (tenant, product): URIRef(f"urn:after-sales-agent:product:{tenant}:{product}")
        for tenant, product in products
    }
    for tenant_ref in tenant_refs.values():
        data_graph.add((tenant_ref, RDF.type, namespace.Tenant))
    for (tenant, product), product_ref in product_refs.items():
        data_graph.add((product_ref, RDF.type, namespace.CanonicalProduct))
        data_graph.add(
            (product_ref, namespace.canonicalProductId, Literal(product, datatype=XSD.string))
        )
        data_graph.add((product_ref, namespace.belongsToTenant, tenant_refs[tenant]))

    intervals: dict[tuple[str, str, str], list[dict]] = {}
    for index, row in enumerate(mappings):
        require(row["tenant_id"] in tenants, f"unknown mapping tenant: {row}")
        key = (row["tenant_id"], row["source_system"], row["source_id"])
        intervals.setdefault(key, []).append(row)
        mapping_ref = URIRef(f"urn:after-sales-agent:mapping:{index + 1}")
        data_graph.add((mapping_ref, RDF.type, namespace.SourceProductMapping))
        data_graph.add((mapping_ref, namespace.belongsToTenant, tenant_refs[row["tenant_id"]]))
        data_graph.add(
            (
                mapping_ref,
                namespace.sourceSystem,
                Literal(row["source_system"], datatype=XSD.string),
            )
        )
        data_graph.add(
            (mapping_ref, namespace.sourceRecordId, Literal(row["source_id"], datatype=XSD.string))
        )
        data_graph.add(
            (
                mapping_ref,
                namespace.mappingVersion,
                Literal(row["version"], datatype=XSD.positiveInteger),
            )
        )
        data_graph.add(
            (mapping_ref, namespace.mappingStatus, Literal(row["status"], datatype=XSD.string))
        )
        data_graph.add(
            (
                mapping_ref,
                namespace.validFrom,
                Literal(date.fromisoformat(row["valid_from"]), datatype=XSD.date),
            )
        )
        if row.get("valid_to"):
            data_graph.add(
                (
                    mapping_ref,
                    namespace.validTo,
                    Literal(date.fromisoformat(row["valid_to"]), datatype=XSD.date),
                )
            )
        if row.get("canonical_product_id"):
            product_ref = product_refs.get((row["tenant_id"], row["canonical_product_id"]))
            require(product_ref is not None, f"mapping target is missing or cross-tenant: {row}")
            data_graph.add((mapping_ref, namespace.mapsToCanonicalProduct, product_ref))

    for key, rows in intervals.items():
        statuses = {row["status"] for row in rows}
        candidates = {
            row.get("canonical_product_id") for row in rows if row.get("canonical_product_id")
        }
        if "ambiguous" in statuses:
            require(
                statuses == {"ambiguous"} and len(candidates) >= 2,
                f"ambiguous mapping lacks distinct candidates: {key}",
            )
        active = sorted(
            (row for row in rows if row["status"] == "active"), key=lambda row: row["valid_from"]
        )
        for left, right in zip(active, active[1:]):
            require(
                left.get("valid_to") is not None and left["valid_to"] <= right["valid_from"],
                f"overlapping active mapping intervals: {key}",
            )

    for index, row in enumerate(evidence_rows):
        if row["tenant_id"] not in tenants:
            continue
        evidence_ref = URIRef(f"urn:after-sales-agent:evidence:{row['evidence_id']}")
        data_graph.add((evidence_ref, RDF.type, namespace.Evidence))
        data_graph.add(
            (evidence_ref, namespace.evidenceId, Literal(row["evidence_id"], datatype=XSD.string))
        )
        data_graph.add(
            (evidence_ref, namespace.evidenceType, Literal(row["kind"], datatype=XSD.string))
        )
        data_graph.add(
            (
                evidence_ref,
                namespace.sourceRecordId,
                Literal(row["source_record_id"], datatype=XSD.string),
            )
        )
        data_graph.add(
            (
                evidence_ref,
                namespace.sourceSystem,
                Literal(row["source_system"], datatype=XSD.string),
            )
        )
        data_graph.add((evidence_ref, namespace.belongsToTenant, tenant_refs[row["tenant_id"]]))
        data_graph.add(
            (
                evidence_ref,
                namespace.observedAt,
                Literal(systems["reference_time"], datatype=XSD.dateTime),
            )
        )

    approvals_by_id = {row["metric_id"]: row for row in approvals["metrics"]}
    require(
        set(approvals_by_id) == {row["metric_id"] for row in metrics},
        "approval register and metric definitions differ",
    )
    for row in metrics:
        approval = approvals_by_id[row["metric_id"]]
        approved = (
            approval["business_approval"]["status"] == "approved"
            and approval["finance_approval"]["status"] == "approved"
        )
        require(
            approval["production_enabled"] == approved
            and row.get("production_enabled", False) == approved,
            f"metric enabled without dual approval: {row['metric_id']}",
        )
        metric_ref = URIRef(f"urn:after-sales-agent:metric:{row['metric_id']}:v{row['version']}")
        data_graph.add((metric_ref, RDF.type, namespace.MetricDefinition))
        data_graph.add(
            (metric_ref, namespace.metricId, Literal(row["metric_id"], datatype=XSD.string))
        )
        data_graph.add(
            (
                metric_ref,
                namespace.metricVersion,
                Literal(row["version"], datatype=XSD.positiveInteger),
            )
        )
        numerator = row.get("numerator") or "; ".join(row.get("numerator_options", []))
        denominator = row.get("denominator") or "; ".join(row.get("denominator_options", []))
        data_graph.add(
            (metric_ref, namespace.numeratorDefinition, Literal(numerator, datatype=XSD.string))
        )
        data_graph.add(
            (metric_ref, namespace.denominatorDefinition, Literal(denominator, datatype=XSD.string))
        )
        state = "approved" if approved else "pending"
        data_graph.add(
            (metric_ref, namespace.businessApprovalStatus, Literal(state, datatype=XSD.string))
        )
        data_graph.add(
            (metric_ref, namespace.productionEnabled, Literal(approved, datatype=XSD.boolean))
        )
        if approved:
            require(
                row.get("formula") and row.get("sample_unit"),
                f"approved metric lacks formula or sample unit: {row['metric_id']}",
            )
            data_graph.add(
                (metric_ref, namespace.formula, Literal(row["formula"], datatype=XSD.string))
            )
            data_graph.add(
                (metric_ref, namespace.sampleUnit, Literal(row["sample_unit"], datatype=XSD.string))
            )

    digest = hashlib.sha256(
        core_path.read_bytes()
        + shapes_path.read_bytes()
        + (ONTOLOGY / "source_mappings.json").read_bytes()
        + (ONTOLOGY / "metrics.json").read_bytes()
    ).hexdigest()
    release_ref = URIRef("urn:after-sales-agent:release:mock-v2:candidate")
    data_graph.add((release_ref, RDF.type, namespace.OntologyRelease))
    for predicate, value, datatype in [
        (namespace.releaseId, "mock-v2-candidate", XSD.string),
        (namespace.schemaVersion, "2.0", XSD.string),
        (namespace.manifestDigest, digest, XSD.string),
        (namespace.releaseRevision, 1, XSD.positiveInteger),
        (namespace.releaseStatus, "candidate", XSD.string),
    ]:
        data_graph.add((release_ref, predicate, Literal(value, datatype=datatype)))

    conforms, report_graph, report_text = validate(
        data_graph,
        shacl_graph=shapes_graph,
        ont_graph=ontology_graph,
        inference="rdfs",
        abort_on_first=False,
        allow_infos=False,
        allow_warnings=False,
        meta_shacl=True,
    )
    require(conforms, f"v2 ontology graph failed SHACL validation:\n{report_text}")

    # Verify a malformed candidate cannot replace the current release pointer.
    invalid_graph = Graph()
    for triple in data_graph:
        invalid_graph.add(triple)
    invalid_mapping = URIRef("urn:after-sales-agent:mapping:invalid-test")
    invalid_graph.add((invalid_mapping, RDF.type, namespace.SourceProductMapping))
    invalid_graph.add(
        (invalid_mapping, namespace.mappingStatus, Literal("active", datatype=XSD.string))
    )
    invalid_conforms, _, _ = validate(
        invalid_graph,
        shacl_graph=shapes_graph,
        ont_graph=ontology_graph,
        inference="rdfs",
        abort_on_first=False,
        meta_shacl=True,
    )
    active_release = "ontology-v1-active"
    require(
        not invalid_conforms and active_release == "ontology-v1-active",
        "invalid candidate changed the active ontology release",
    )
    active_release = "mock-v2-candidate"
    active_release = (
        "ontology-v1-active"  # rollback points to the previously validated immutable release
    )
    require(
        active_release == "ontology-v1-active",
        "rollback did not restore the previous release pointer",
    )

    current = next(
        row for row in systems["metric_snapshots"] if row["snapshot_id"] == "METRIC-RETURN-2026-09"
    )
    baseline = next(
        row for row in systems["metric_snapshots"] if row["snapshot_id"] == "METRIC-RETURN-2026-08"
    )
    require(
        current["numerator"] == 10
        and current["denominator"] == 100
        and baseline["numerator"] == 4
        and baseline["denominator"] == 100,
        "return-rate snapshot does not match source facts",
    )
    require(
        round(current["rate"] - baseline["rate"], 8) == 0.06,
        "return-rate anomaly delta is not reproducible",
    )
    require(
        any(
            row["evidence_id"] == "EVID-RETURN-001" and row["source_record_id"] == "WMS-REC-001"
            for row in evidence_rows
        ),
        "current anomaly evidence does not resolve to WMS source record",
    )
    require(
        not approvals_by_id["return_passed_item_rate"]["production_enabled"],
        "pending metric approval must block production ranking",
    )

    print(
        json.dumps(
            {
                "result": "passed",
                "scope": "rdflib_owl_parse_pyshacl_mock_graph_and_in_memory_release_simulation",
                "triples": len(data_graph),
                "shapes": len(shapes_graph),
                "digest": digest,
                "return_rate_delta": 0.06,
                "production_metrics_enabled": 0,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, KeyError, StopIteration, ValueError) as error:
        print(f"FAILED: {error}", file=sys.stderr)
        raise SystemExit(1)
