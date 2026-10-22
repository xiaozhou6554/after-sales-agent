"""步骤05迁移测试：空基线版本、发现与排序、重复执行不再产生变更。"""

import pytest
from bootstrap.migrate import compute_pending, discover_versions, run_apply, run_status
from migrations.versions.v0001_baseline import VERSION, statements


class FakeExecutor:
    """内存迁移执行器：记录语句与版本，模拟真实 MySQL 记账行为。"""

    def __init__(self) -> None:
        self.statements_run: list[str] = []
        self.recorded: list[str] = []
        self.bookkeeping_created = 0

    def ensure_bookkeeping(self) -> None:
        self.bookkeeping_created += 1

    def applied_versions(self) -> list[str]:
        return list(self.recorded)

    def run_statements(self, statements: list[str]) -> None:
        self.statements_run.extend(statements)

    def record_version(self, version: str) -> None:
        self.recorded.append(version)


def test_baseline_is_empty_and_well_formed() -> None:
    assert VERSION == "0001"
    assert statements() == []


def test_discover_versions_sorted_and_valid() -> None:
    versions = [str(module.VERSION) for module in discover_versions()]
    assert versions == sorted(versions)
    assert "0001" in versions


def test_compute_pending_and_unknown_version_guard() -> None:
    assert compute_pending(["0001", "0002"], []) == ["0001", "0002"]
    assert compute_pending(["0001", "0002"], ["0001"]) == ["0002"]
    assert compute_pending(["0001"], ["0001"]) == []
    with pytest.raises(ValueError, match="未知迁移版本"):
        compute_pending(["0002"], ["0001"])


def test_apply_applies_all_versions_in_order_and_records() -> None:
    executor = FakeExecutor()
    applied = run_apply(executor)
    versions = [str(module.VERSION) for module in discover_versions()]
    assert applied == len(versions)
    assert executor.recorded == versions
    assert executor.bookkeeping_created == 1
    # 空基线不含任何业务表语句；mock_ 业务表 DDL 全部幂等（IF NOT EXISTS）。
    # 仅统计建表目标为 mock_ 表的语句；引用 mock_ 表的 FK/索引 DDL 不在内。
    mock_statements = [
        statement
        for statement in executor.statements_run
        if statement.startswith("CREATE TABLE IF NOT EXISTS mock_")
    ]
    assert mock_statements


def test_repeat_apply_is_noop() -> None:
    executor = FakeExecutor()
    versions = discover_versions()
    assert run_apply(executor) == len(versions)
    assert run_apply(executor) == 0
    assert run_apply(executor) == 0
    assert executor.recorded == [str(module.VERSION) for module in versions]
    assert len(executor.statements_run) == sum(len(module.statements()) for module in versions)


def test_run_status_reports_applied_and_pending(capsys) -> None:
    executor = FakeExecutor()
    versions = [str(module.VERSION) for module in discover_versions()]
    run_status(executor)
    out = capsys.readouterr().out
    assert "已应用版本：无" in out
    assert f"待应用版本：{versions}" in out

    assert run_apply(executor) == len(versions)
    run_status(executor)
    out = capsys.readouterr().out
    assert f"已应用版本：{versions}" in out
    assert "待应用版本：无" in out
