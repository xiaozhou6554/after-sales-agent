"""显式数据库迁移：查看版本与升级，不随进程启动执行。

机制：
- 迁移版本为 ``migrations/versions/`` 下的模块，按 VERSION 升序执行；
- 版本记录保存在 MySQL ``schema_migrations`` 表（由基础设施执行器负责建表）；
- MySQL DDL 隐式提交，无法整体回滚：所有 DDL 语句必须写成幂等
  （CREATE TABLE IF NOT EXISTS 等），重复执行安全；
- API/MCP/Worker 启动流程绝不调用本模块，避免多进程争抢结构升级；
- 语句中不得使用含分号的存储过程/触发器体。

用法：
    python -m bootstrap.migrate status   # 查看已应用与待应用版本
    python -m bootstrap.migrate apply    # 按顺序应用待执行版本
"""

from __future__ import annotations

import argparse
import importlib
import pkgutil
import re
import sys
from types import ModuleType
from typing import Protocol

from bootstrap.settings import RuntimeSettings, load_settings
from infrastructure.persistence.mysql.connection import create_mysql_engine
from infrastructure.persistence.mysql.migration_runner import MySQLMigrationRunner

_VERSION_PATTERN = re.compile(r"^\d{4}$")


def discover_versions() -> list[ModuleType]:
    """发现并按 VERSION 升序返回迁移模块；重复、缺失或非法 VERSION 视为错误。"""
    package = importlib.import_module("migrations.versions")
    modules: dict[str, ModuleType] = {}
    for info in pkgutil.iter_modules(package.__path__):
        if not info.name.startswith("v"):
            continue
        module = importlib.import_module(f"migrations.versions.{info.name}")
        version = getattr(module, "VERSION", None)
        if not version:
            raise ValueError(f"迁移模块 {info.name} 缺少 VERSION")
        if not _VERSION_PATTERN.match(str(version)):
            raise ValueError(f"迁移版本号必须为4位数字：{info.name} -> {version!r}")
        if version in modules:
            raise ValueError(f"迁移版本重复：{version}")
        modules[version] = module
    return [modules[key] for key in sorted(modules)]


def compute_pending(defined: list[str], applied: list[str]) -> list[str]:
    """计算待应用版本；已应用版本必须全部在已定义集合中，否则拒绝执行。"""
    defined_set = set(defined)
    unknown = [version for version in applied if version not in defined_set]
    if unknown:
        raise ValueError(f"数据库存在未知迁移版本：{sorted(unknown)}")
    applied_set = set(applied)
    return [version for version in defined if version not in applied_set]


class MigrationExecutor(Protocol):
    """迁移执行所需的最小能力；真实实现为基础设施层 MySQLMigrationRunner。"""

    def ensure_bookkeeping(self) -> None: ...
    def applied_versions(self) -> list[str]: ...
    def run_statements(self, statements: list[str]) -> None: ...
    def record_version(self, version: str) -> None: ...


def run_apply(executor: MigrationExecutor, log=print) -> int:
    executor.ensure_bookkeeping()
    modules = {str(module.VERSION): module for module in discover_versions()}
    pending = compute_pending(list(modules), executor.applied_versions())
    if not pending:
        log("无待应用迁移。")
        return 0
    for version in pending:
        log(f"应用迁移 {version} ...")
        executor.run_statements(modules[version].statements())
        executor.record_version(version)
        log(f"迁移 {version} 完成。")
    return len(pending)


def run_status(executor: MigrationExecutor, log=print) -> None:
    executor.ensure_bookkeeping()
    defined = [str(module.VERSION) for module in discover_versions()]
    pending = compute_pending(defined, executor.applied_versions())
    log(f"已应用版本：{executor.applied_versions() or '无'}")
    log(f"待应用版本：{pending or '无'}")


def build_executor(settings: RuntimeSettings) -> MySQLMigrationRunner:
    engine = create_mysql_engine(
        settings.mysql_url,
        settings.mysql_connect_timeout_s,
        settings.mysql_read_timeout_s,
    )
    return MySQLMigrationRunner(engine)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="显式数据库迁移（不随进程启动执行）")
    parser.add_argument("command", choices=["status", "apply"])
    args = parser.parse_args(argv)
    settings = load_settings()
    runner = build_executor(settings)
    try:
        if args.command == "status":
            run_status(runner)
        else:
            run_apply(runner)
    except Exception as exc:  # 输出脱敏错误，不暴露连接串。
        print(f"迁移失败：{settings.mask_text(str(exc))}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
