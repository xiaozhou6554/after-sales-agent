"""OWL/SHACL 校验适配器：rdflib 解析冻结词汇，pySHACL 校验候选数据图。

生产基线固定 rdflib/pyshacl 版本（见 data/requirements-ontology.txt）；
校验报告作为发布产物留存，校验失败不得改动当前激活指针。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ONTOLOGY_DIR = ROOT / "data" / "knowledge" / "v2" / "ontology"
NAMESPACE = "urn:after-sales-agent:ontology:v2#"


@dataclass(frozen=True)
class ValidationResult:
    conforms: bool
    report_text: str
    manifest_digest: str


class OntologyValidator:
    """对候选映射/指标/发布清单执行 OWL 2 结构解析与 SHACL 约束校验。"""

    def __init__(self, ontology_dir: Path = ONTOLOGY_DIR) -> None:
        self._dir = ontology_dir

    def validate_candidate(
        self,
        mappings: list[dict],
        products: list[dict],
        metrics: list[dict],
        metric_approvals: dict,
        release: dict,
        extra_quads: list[tuple] | None = None,
    ) -> ValidationResult:
        from pyshacl import validate
        from rdflib import RDF, XSD, Graph, Literal, Namespace, URIRef

        namespace = Namespace(NAMESPACE)
        ontology_graph = Graph().parse(self._dir / "core.ttl", format="turtle")
        shapes_graph = Graph().parse(self._dir / "constraints.shacl.ttl", format="turtle")
        data_graph = Graph()
        data_graph.bind("", namespace)

        tenants = {row["tenant_id"] for row in products}
        tenant_refs = {
            tenant: URIRef(f"urn:after-sales-agent:tenant:{tenant}") for tenant in tenants
        }
        for tenant_ref in tenant_refs.values():
            data_graph.add((tenant_ref, RDF.type, namespace.Tenant))
        product_refs: dict[tuple[str, str], URIRef] = {}
        for row in products:
            ref = URIRef(
                f"urn:after-sales-agent:product:{row['tenant_id']}:{row['canonical_product_id']}"
            )
            product_refs[(row["tenant_id"], row["canonical_product_id"])] = ref
            data_graph.add((ref, RDF.type, namespace.CanonicalProduct))
            data_graph.add((
                ref,
                namespace.canonicalProductId,
                Literal(row["canonical_product_id"], datatype=XSD.string),
            ))
            data_graph.add((ref, namespace.belongsToTenant, tenant_refs[row["tenant_id"]]))

        for row in mappings:
            tenant = row["tenant_id"]
            target = row.get("canonical_product_id")
            product_ref = product_refs.get((tenant, target)) if target else None
            if target and product_ref is None:
                # 跨租户/缺失目标：不进入图，SHACL 的 active 恰一目标约束必然失败。
                continue
            mapping_ref = URIRef(
                f"urn:after-sales-agent:mapping:{tenant}:{row['source_system']}:"
                f"{row['source_id']}:{row['version']}:{target or 'none'}"
            )
            data_graph.add((mapping_ref, RDF.type, namespace.SourceProductMapping))
            data_graph.add(
                (mapping_ref, namespace.belongsToTenant, tenant_refs[tenant])
            )
            data_graph.add((
                mapping_ref,
                namespace.sourceSystem,
                Literal(row["source_system"], datatype=XSD.string),
            ))
            data_graph.add((
                mapping_ref,
                namespace.sourceRecordId,
                Literal(row["source_id"], datatype=XSD.string),
            ))
            data_graph.add((
                mapping_ref,
                namespace.mappingVersion,
                Literal(int(row["version"]), datatype=XSD.positiveInteger),
            ))
            data_graph.add((
                mapping_ref,
                namespace.mappingStatus,
                Literal(row["status"], datatype=XSD.string),
            ))
            data_graph.add((
                mapping_ref,
                namespace.validFrom,
                Literal(date.fromisoformat(row["valid_from"]), datatype=XSD.date),
            ))
            if row.get("valid_to"):
                data_graph.add((
                    mapping_ref,
                    namespace.validTo,
                    Literal(date.fromisoformat(row["valid_to"]), datatype=XSD.date),
                ))
            if product_ref is not None:
                data_graph.add(
                    (mapping_ref, namespace.mapsToCanonicalProduct, product_ref)
                )

        approvals_by_id = {
            row["metric_id"]: row for row in metric_approvals.get("metrics", [])
        }
        for row in metrics:
            approval = approvals_by_id.get(row["metric_id"])
            approved = bool(
                approval
                and approval["business_approval"]["status"] == "approved"
                and approval["finance_approval"]["status"] == "approved"
            )
            metric_ref = URIRef(
                f"urn:after-sales-agent:metric:{row['metric_id']}:v{row['version']}"
            )
            data_graph.add((metric_ref, RDF.type, namespace.MetricDefinition))
            data_graph.add((
                metric_ref,
                namespace.metricId,
                Literal(row["metric_id"], datatype=XSD.string),
            ))
            data_graph.add((
                metric_ref,
                namespace.metricVersion,
                Literal(int(row["version"]), datatype=XSD.positiveInteger),
            ))
            numerator = row.get("numerator") or "; ".join(row.get("numerator_options", []))
            denominator = row.get("denominator") or "; ".join(
                row.get("denominator_options", [])
            )
            data_graph.add((
                metric_ref,
                namespace.numeratorDefinition,
                Literal(numerator, datatype=XSD.string),
            ))
            data_graph.add((
                metric_ref,
                namespace.denominatorDefinition,
                Literal(denominator, datatype=XSD.string),
            ))
            state = "approved" if approved else "pending"
            data_graph.add((
                metric_ref,
                namespace.businessApprovalStatus,
                Literal(state, datatype=XSD.string),
            ))
            data_graph.add((
                metric_ref,
                namespace.productionEnabled,
                Literal(approved, datatype=XSD.boolean),
            ))
            if approved:
                data_graph.add((
                    metric_ref,
                    namespace.formula,
                    Literal(row.get("formula"), datatype=XSD.string),
                ))
                data_graph.add((
                    metric_ref,
                    namespace.sampleUnit,
                    Literal(row.get("sample_unit"), datatype=XSD.string),
                ))

        release_ref = URIRef(f"urn:after-sales-agent:release:{release['release_id']}")
        data_graph.add((release_ref, RDF.type, namespace.OntologyRelease))
        for predicate, value, datatype in (
            (namespace.releaseId, release["release_id"], XSD.string),
            (namespace.schemaVersion, release["schema_version"], XSD.string),
            (namespace.manifestDigest, release["manifest_digest"], XSD.string),
            (namespace.releaseRevision, int(release["revision"]), XSD.positiveInteger),
            (namespace.releaseStatus, release["status"], XSD.string),
        ):
            data_graph.add(
                (release_ref, predicate, Literal(value, datatype=datatype))
            )

        if extra_quads:
            for subject, predicate, obj in extra_quads:
                data_graph.add((subject, predicate, obj))

        import hashlib

        digest = hashlib.sha256()
        for name in ("core.ttl", "constraints.shacl.ttl", "source_mappings.json",
                     "metrics.json"):
            digest.update((self._dir / name).read_bytes())
        file_digest = digest.hexdigest()

        conforms, _report_graph, report_text = validate(
            data_graph,
            shacl_graph=shapes_graph,
            ont_graph=ontology_graph,
            inference="rdfs",
            abort_on_first=False,
            allow_infos=False,
            allow_warnings=False,
            meta_shacl=True,
        )
        return ValidationResult(conforms, report_text, file_digest)
