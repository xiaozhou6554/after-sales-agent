"""因素贡献计算（FR-24/TC-111/114）：分组复算、百分点贡献、可比性门。

贡献排序必须来自"组率差 × 组份额"，不得用组率排序替代；样本不足或基准
不可比时停止排名并给出原因。原因标签只使用结构化键，不做自由文本因果。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal


@dataclass(frozen=True)
class FactorGroup:
    cause_code: str
    numerator: int
    denominator: int

    @property
    def rate(self) -> Decimal | None:
        if self.denominator == 0:
            return None
        return (Decimal(self.numerator) / Decimal(self.denominator)).quantize(
            Decimal("0.00000001"), rounding=ROUND_HALF_UP
        )


@dataclass(frozen=True)
class Contribution:
    cause_code: str
    numerator_now: int
    denominator_now: int
    rate_now: Decimal | None
    numerator_base: int
    denominator_base: int
    rate_base: Decimal | None
    share_of_denominator: Decimal
    contribution_pp: Decimal | None


def percentage_points(now_rate: Decimal | None, base_rate: Decimal | None
                      ) -> Decimal | None:
    if now_rate is None or base_rate is None:
        return None
    return ((now_rate - base_rate) * 100).quantize(
        Decimal("0.0001"), rounding=ROUND_HALF_UP
    )


def factor_contributions(
    now: list[FactorGroup], base: list[FactorGroup]
) -> list[Contribution]:
    """按组复算贡献：contribution_pp = (组率差) × 组分母份额 × 100。

    基准缺失的组按基准率 0 处理（新增组的出现本身是贡献来源）；
    当前分母为 0 的组贡献为 None（不可计算，不伪造 0）。
    """
    total_den = sum(group.denominator for group in now)
    base_by_code = {group.cause_code: group for group in base}
    results: list[Contribution] = []
    for group in now:
        counterpart = base_by_code.get(group.cause_code)
        # 基准缺失的组按基准率 0 参与计算（新增组出现本身即贡献）；
        # 汇报侧 rate_base 保持 None，不伪造基准。
        math_base_rate = (
            counterpart.rate if counterpart is not None else Decimal("0")
        )
        share = (
            Decimal(group.denominator) / Decimal(total_den)
            if total_den > 0 else None
        )
        contribution = None
        delta = percentage_points(group.rate, math_base_rate)
        if delta is not None and share is not None:
            contribution = (delta * share).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )
        results.append(Contribution(
            cause_code=group.cause_code,
            numerator_now=group.numerator,
            denominator_now=group.denominator,
            rate_now=group.rate,
            numerator_base=counterpart.numerator if counterpart else 0,
            denominator_base=counterpart.denominator if counterpart else 0,
            rate_base=counterpart.rate if counterpart is not None else None,
            share_of_denominator=(
                share.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
                if share is not None else Decimal("0")
            ),
            contribution_pp=contribution,
        ))
    results.sort(
        key=lambda item: (
            abs(item.contribution_pp) if item.contribution_pp is not None
            else Decimal("-1")
        ),
        reverse=True,
    )
    return results


def comparability_gate(
    *,
    now_quality: str | None,
    base_quality: str | None,
    now_denominator: int | None,
    base_denominator: int | None,
    minimum_sample: int,
) -> tuple[bool, list[str]]:
    """质量门（TC-113/114）：任一窗口不合格即停止排名并说明原因。"""
    issues: list[str] = []
    if now_quality is None:
        issues.append("current_snapshot_missing")
    elif now_quality != "complete":
        issues.append(f"current_quality_{now_quality}")
    if base_quality is None:
        issues.append("baseline_snapshot_missing")
    elif base_quality != "complete":
        issues.append(f"baseline_quality_{base_quality}")
    if now_denominator is not None and now_denominator < minimum_sample:
        issues.append("current_insufficient_sample")
    if base_denominator is not None and base_denominator < minimum_sample:
        issues.append("baseline_insufficient_sample")
    return (not issues), issues
