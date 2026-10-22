from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from application.knowledge.policy import PolicyDocument, PolicyReader
from infrastructure.document.policy_catalog import LocalManifestPolicySource


class Source:
    def __init__(self, *items):
        self.items = items

    def list_for_release(self, tenant_id, knowledge_release_id):
        return tuple(
            item
            for item in self.items
            if item.tenant_id == tenant_id and item.knowledge_release_id == knowledge_release_id
        )


def policy(**changes):
    item = PolicyDocument(
        "tenant-north",
        "release-v1",
        "POL-RETURN-01",
        "DOC-RETURN",
        "退货政策",
        ("退货",),
        ("签收后七日内可申请退货",),
        (),
        date(2026, 1, 1),
        None,
        "2",
        "ev-current",
        "published",
    )
    return replace(item, **changes)


def test_exact_name_alias_and_stable_id_resolve_only_effective_version() -> None:
    current = policy()
    legacy = policy(
        document_id="DOC-LEGACY",
        version="1",
        evidence_id="ev-old",
        valid_from=date(2025, 1, 1),
        valid_to=date(2025, 12, 31),
        status="retired",
    )
    draft = policy(document_id="DOC-DRAFT", version="3", evidence_id="ev-draft", status="draft")
    reader = PolicyReader(Source(current, legacy, draft))
    for identifier in ("退货政策", "退货", "POL-RETURN-01"):
        result = reader.get(
            identifier,
            tenant_id="tenant-north",
            knowledge_release_id="release-v1",
            as_of=date(2026, 10, 21),
        )
        assert result.match_status == "found"
        assert result.policy.evidence_refs == ("ev-current",)
        assert result.policy.clauses == ("签收后七日内可申请退货",)
    historical = reader.get(
        "POL-RETURN-01",
        tenant_id="tenant-north",
        knowledge_release_id="release-v1",
        as_of=date(2025, 6, 1),
    )
    assert historical.policy.evidence_refs == ("ev-old",)


def test_ambiguous_name_is_not_silently_selected() -> None:
    reader = PolicyReader(Source(policy(), policy(policy_id="POL-OTHER", evidence_id="ev-other")))
    result = reader.get(
        "退货政策",
        tenant_id="tenant-north",
        knowledge_release_id="release-v1",
        as_of=date(2026, 10, 21),
    )
    assert result.match_status == "ambiguous"
    assert result.policy is None
    assert {item.policy_id for item in result.candidates} == {"POL-RETURN-01", "POL-OTHER"}


def test_scope_mismatch_fails_closed_and_draft_never_matches() -> None:
    class BadSource:
        def list_for_release(self, *_):
            return (policy(tenant_id="tenant-south"),)

    with pytest.raises(ValueError, match="越界"):
        PolicyReader(BadSource()).get(
            "退货政策",
            tenant_id="tenant-north",
            knowledge_release_id="release-v1",
            as_of=date(2026, 10, 21),
        )
    result = PolicyReader(Source(policy(status="draft"))).get(
        "退货政策",
        tenant_id="tenant-north",
        knowledge_release_id="release-v1",
        as_of=date(2026, 10, 21),
    )
    assert result.match_status == "not_found"


def test_local_policy_source_checks_manifest_and_tenant() -> None:
    root = Path(__file__).resolve().parents[2]
    source = LocalManifestPolicySource(
        root / "data/knowledge/v2/corpus.json", root / "data/manifests/knowledge-v2.json"
    )
    reader = PolicyReader(source)
    north = reader.get(
        "退货政策",
        tenant_id="tenant-north",
        knowledge_release_id="KNOWLEDGE-RELEASE-2026-10",
        as_of=date(2026, 10, 21),
    )
    south = reader.get(
        "退货政策",
        tenant_id="tenant-south",
        knowledge_release_id="KNOWLEDGE-RELEASE-2026-10",
        as_of=date(2026, 10, 21),
    )
    assert north.policy.evidence_refs == ("KNOW-EV-RETURN-02",)
    assert south.match_status == "not_found"
