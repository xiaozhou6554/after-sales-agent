"""步骤25D 纯域单测：因素贡献排序与可比性门（TC-111/113/114）。"""

from decimal import Decimal

import pytest

from domain.investigation.contribution import (
    FactorGroup,
    comparability_gate,
    factor_contributions,
    percentage_points,
)


class TestPercentagePoints:
    def test_rate_diff_times_100(self):
        assert percentage_points(Decimal("0.25"), Decimal("0.10")) == Decimal("15.0000")

    def test_none_rate_propagates(self):
        assert percentage_points(None, Decimal("0.1")) is None
        assert percentage_points(Decimal("0.1"), None) is None


class TestFactorContributions:
    def test_contribution_is_rate_gap_times_share(self):
        # 当前：A 30/100（30%），B 5/100（5%）；基准：A 10/100（10%），B 20/100（20%）。
        now = [FactorGroup("group:a", 30, 100), FactorGroup("group:b", 5, 100)]
        base = [FactorGroup("group:a", 10, 100), FactorGroup("group:b", 20, 100)]
        results = factor_contributions(now, base)
        by_code = {item.cause_code: item for item in results}
        # A：率差 20pp × 份额 0.5 = 10pp；B：率差 -15pp × 0.5 = -7.5pp。
        assert by_code["group:a"].contribution_pp == Decimal("10.0000")
        assert by_code["group:b"].contribution_pp == Decimal("-7.5000")

    def test_ordering_by_contribution_not_rate(self):
        # TC-111：组率最高的组不是贡献最大的组，排序必须按贡献绝对值。
        now = [
            FactorGroup("group:high_rate", 90, 100),   # 率 90%，率差仅 +5pp
            FactorGroup("group:big_gap", 10, 400),     # 率 2.5%，率差 -47.5pp
        ]
        base = [
            FactorGroup("group:high_rate", 85, 100),
            FactorGroup("group:big_gap", 200, 400),    # 率 50%
        ]
        results = factor_contributions(now, base)
        # high_rate 贡献 = 5pp × 0.2 = 1pp；big_gap 贡献 = -47.5pp × 0.8 = -38pp。
        assert results[0].cause_code == "group:big_gap"
        assert results[0].contribution_pp == Decimal("-38.0000")
        assert results[1].contribution_pp == Decimal("1.0000")

    def test_missing_base_group_treated_as_zero_rate(self):
        # 新增组按基准率 0 参与计算；汇报侧 rate_base 保持 None 不伪造基准。
        now = [FactorGroup("group:new", 50, 100), FactorGroup("group:old", 0, 100)]
        base = [FactorGroup("group:old", 10, 100)]
        results = factor_contributions(now, base)
        by_code = {item.cause_code: item for item in results}
        assert by_code["group:new"].rate_base is None
        # 率差 50pp × 份额 0.5 = 25pp。
        assert by_code["group:new"].contribution_pp == Decimal("25.0000")
        assert by_code["group:old"].rate_base == Decimal("0.10000000")

    def test_zero_denominator_group_contribution_is_none(self):
        # 分母为 0 的组不伪造 0 贡献，如实返回 None 并沉底。
        now = [FactorGroup("group:a", 10, 100), FactorGroup("group:empty", 0, 0)]
        base = [FactorGroup("group:a", 5, 100), FactorGroup("group:empty", 0, 0)]
        results = factor_contributions(now, base)
        assert results[0].cause_code == "group:a"
        assert results[-1].cause_code == "group:empty"
        assert results[-1].contribution_pp is None

    def test_total_denominator_zero_all_none(self):
        results = factor_contributions(
            [FactorGroup("group:a", 3, 0)], [FactorGroup("group:a", 1, 5)]
        )
        assert results[0].contribution_pp is None


class TestComparabilityGate:
    def test_comparable_when_complete_and_sufficient(self):
        ok, issues = comparability_gate(
            now_quality="complete", base_quality="complete",
            now_denominator=100, base_denominator=100, minimum_sample=30,
        )
        assert ok is True
        assert issues == []

    def test_blocks_non_complete_quality(self):
        ok, issues = comparability_gate(
            now_quality="watermark_lag", base_quality="complete",
            now_denominator=100, base_denominator=100, minimum_sample=30,
        )
        assert ok is False
        assert issues == ["current_quality_watermark_lag"]

    def test_blocks_insufficient_sample_both_sides(self):
        ok, issues = comparability_gate(
            now_quality="complete", base_quality="complete",
            now_denominator=29, base_denominator=5, minimum_sample=30,
        )
        assert ok is False
        assert set(issues) == {
            "current_insufficient_sample", "baseline_insufficient_sample"
        }

    def test_blocks_missing_snapshots(self):
        ok, issues = comparability_gate(
            now_quality=None, base_quality="complete",
            now_denominator=None, base_denominator=100, minimum_sample=1,
        )
        assert ok is False
        assert issues == ["current_snapshot_missing"]

    def test_accumulates_all_issues(self):
        ok, issues = comparability_gate(
            now_quality="zero_denominator", base_quality="immature",
            now_denominator=0, base_denominator=1, minimum_sample=30,
        )
        assert ok is False
        assert issues == [
            "current_quality_zero_denominator",
            "baseline_quality_immature",
            "current_insufficient_sample",
            "baseline_insufficient_sample",
        ]

    @pytest.mark.parametrize("quality", [
        "complete", "zero_denominator", "insufficient_sample", "immature",
        "watermark_lag", "reconciliation_difference",
    ])
    def test_only_complete_passes(self, quality):
        ok, _ = comparability_gate(
            now_quality=quality, base_quality=quality,
            now_denominator=100, base_denominator=100, minimum_sample=30,
        )
        assert ok is (quality == "complete")
