"""时态商品映射规则（FR-20/TC-95/96）：状态-目标数量、有效期不重叠、租户边界。

同名不同SKU不得合并；跨租户引用一律违规；ambiguous/缺失不得作为成功事实。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

STATUSES = ("active", "ambiguous", "unmapped", "expired")

ACTIVE = "active"
AMBIGUOUS = "ambiguous"
UNRESOLVED = ("unmapped", "expired")


@dataclass(frozen=True)
class MappingRecord:
    tenant_id: str
    source_system: str
    source_id: str
    version: int
    canonical_product_id: str | None
    valid_from: date
    valid_to: date | None
    status: str


@dataclass(frozen=True)
class MappingViolation:
    violation_type: str
    tenant_id: str
    source_system: str
    source_id: str
    detail: str


@dataclass(frozen=True)
class Resolution:
    canonical_product_id: str | None
    status: str
    mapping_version: int | None


@dataclass(frozen=True)
class _Key:
    tenant_id: str
    source_system: str
    source_id: str


def _covers(row: MappingRecord, on_date: date) -> bool:
    if row.valid_from > on_date:
        return False
    return row.valid_to is None or on_date < row.valid_to


def validate_mapping_set(
    records: list[MappingRecord],
    tenant_products: dict[str, set[str]],
) -> list[MappingViolation]:
    """校验整套映射：状态枚举、状态-目标数量、有效期不重叠、租户边界、版本完整。"""
    violations: list[MappingViolation] = []
    grouped: dict[_Key, list[MappingRecord]] = {}
    for row in records:
        if row.status not in STATUSES:
            violations.append(MappingViolation(
                "unknown_state", row.tenant_id, row.source_system, row.source_id,
                f"未知映射状态: {row.status}",
            ))
            continue
        products = tenant_products.get(row.tenant_id, set())
        if row.canonical_product_id and row.canonical_product_id not in products:
            # TC-96：目标商品不存在或属于其他租户，均按租户边界违规拒绝。
            violations.append(MappingViolation(
                "cross_tenant_or_missing_target", row.tenant_id, row.source_system,
                row.source_id, f"目标商品不在本租户: {row.canonical_product_id}",
            ))
        grouped.setdefault(
            _Key(row.tenant_id, row.source_system, row.source_id), []
        ).append(row)
    for key, rows in grouped.items():
        statuses = {row.status for row in rows}
        targets = {row.canonical_product_id for row in rows if row.canonical_product_id}
        if statuses == {AMBIGUOUS}:
            if len(targets) < 2:
                violations.append(MappingViolation(
                    "ambiguous_needs_two", key.tenant_id, key.source_system,
                    key.source_id, f"歧义候选不足: {sorted(targets)}",
                ))
        elif statuses <= set(UNRESOLVED):
            if targets:
                violations.append(MappingViolation(
                    "unresolved_with_target", key.tenant_id, key.source_system,
                    key.source_id, f"未解析映射不得携带目标: {sorted(targets)}",
                ))
        elif ACTIVE in statuses:
            active = [row for row in rows if row.status == ACTIVE]
            for row in active:
                if not row.canonical_product_id:
                    # SHACL：active 映射记录必须恰有一个目标。
                    violations.append(MappingViolation(
                        "active_needs_exactly_one", key.tenant_id, key.source_system,
                        key.source_id, f"active 记录缺少目标（v{row.version}）",
                    ))
            ordered = sorted(active, key=lambda row: row.valid_from)
            for left, right in zip(ordered, ordered[1:]):
                if left.valid_to is None or left.valid_to > right.valid_from:
                    violations.append(MappingViolation(
                        "overlapping_validity", key.tenant_id, key.source_system,
                        key.source_id,
                        f"[{left.valid_from}~{left.valid_to}] 与"
                        f" [{right.valid_from}~{right.valid_to}] 重叠",
                    ))
            same_version = {}
            for row in rows:
                same_version.setdefault(row.version, set()).add(
                    (row.canonical_product_id, row.valid_from, row.valid_to)
                )
            for version, variants in same_version.items():
                if len(variants) > 1:
                    violations.append(MappingViolation(
                        "version_conflict", key.tenant_id, key.source_system,
                        key.source_id, f"版本 {version} 存在多个不同事实",
                    ))
    return violations


def resolve_mapping(rows: list[MappingRecord], on_date: date) -> Resolution:
    """在时点解析一个源键；恰好一个 active 生效才返回成功事实。"""
    covering = [row for row in rows if _covers(row, on_date)]
    active = [row for row in covering if row.status == ACTIVE]
    if len(active) == 1:
        return Resolution(active[0].canonical_product_id, ACTIVE, active[0].version)
    if len(active) > 1:
        targets = {row.canonical_product_id for row in active}
        if len(targets) == 1:
            return Resolution(targets.pop(), ACTIVE, max(row.version for row in active))
        return Resolution(None, AMBIGUOUS, None)
    if any(row.status == AMBIGUOUS for row in covering):
        return Resolution(None, AMBIGUOUS, None)
    if covering:
        return Resolution(None, covering[0].status, None)
    return Resolution(None, "unmapped", None)
