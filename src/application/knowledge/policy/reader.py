"""政策名称/稳定ID查找；有效期、草稿和同名歧义均 fail closed。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol


@dataclass(frozen=True)
class PolicyDocument:
    tenant_id: str
    knowledge_release_id: str
    policy_id: str
    document_id: str
    policy_name: str
    aliases: tuple[str, ...]
    clauses: tuple[str, ...]
    applicability: tuple[str, ...]
    valid_from: date
    valid_to: date | None
    version: str
    evidence_id: str
    status: str  # published / retired / draft


class PolicySourcePort(Protocol):
    def list_for_release(
        self, tenant_id: str, knowledge_release_id: str
    ) -> tuple[PolicyDocument, ...]: ...


@dataclass(frozen=True)
class PolicyDetail:
    policy_id: str
    policy_name: str
    clauses: tuple[str, ...]
    applicability: tuple[str, ...]
    valid_from: date
    valid_to: date | None
    version: str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class PolicyCandidate:
    policy_id: str
    policy_name: str
    evidence_id: str


@dataclass(frozen=True)
class PolicyResult:
    match_status: str  # found / ambiguous / not_found
    policy: PolicyDetail | None
    candidates: tuple[PolicyCandidate, ...]
    knowledge_release_id: str


class PolicyReader:
    def __init__(self, source: PolicySourcePort) -> None:
        self._source = source

    def get(
        self,
        identifier: str,
        *,
        tenant_id: str,
        knowledge_release_id: str,
        as_of: date,
    ) -> PolicyResult:
        if not identifier.strip() or not tenant_id or not knowledge_release_id:
            raise ValueError("政策名称、租户和发布版本必填")
        needle = identifier.strip().casefold()
        records = self._source.list_for_release(tenant_id, knowledge_release_id)
        matches = []
        for record in records:
            if record.tenant_id != tenant_id or record.knowledge_release_id != knowledge_release_id:
                raise ValueError("政策源返回越界记录")
            if record.status not in ("published", "retired"):
                continue
            if record.valid_from > as_of or (
                record.valid_to is not None and as_of > record.valid_to
            ):
                continue
            names = (record.policy_id, record.policy_name, *record.aliases)
            if any(name.casefold() == needle for name in names):
                matches.append(record)
        if not matches:
            return PolicyResult("not_found", None, (), knowledge_release_id)
        matches.sort(key=lambda item: (item.policy_id, item.version, item.document_id))
        if len(matches) != 1:
            return PolicyResult(
                "ambiguous",
                None,
                tuple(
                    PolicyCandidate(item.policy_id, item.policy_name, item.evidence_id)
                    for item in matches
                ),
                knowledge_release_id,
            )
        item = matches[0]
        return PolicyResult(
            "found",
            PolicyDetail(
                item.policy_id,
                item.policy_name,
                item.clauses,
                item.applicability,
                item.valid_from,
                item.valid_to,
                item.version,
                (item.evidence_id,),
            ),
            (),
            knowledge_release_id,
        )
