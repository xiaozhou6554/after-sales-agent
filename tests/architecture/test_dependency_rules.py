"""步骤04架构依赖检查测试：真实代码树通过、规则引擎捕获违规、临时违规可被发现与撤销。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from check_dependencies import check_source, scan_project


def test_real_tree_has_no_violations() -> None:
    assert scan_project() == []


def test_domain_must_be_pure_python() -> None:
    assert check_source("import decimal\n", layer="domain") == []
    found = check_source("import pydantic\n", layer="domain")
    assert len(found) == 1 and found[0].rule == "third-party"
    found = check_source("import sqlalchemy\n", layer="domain")
    assert len(found) == 1 and found[0].rule == "third-party"


def test_domain_forbids_other_layers() -> None:
    for top in ("application", "interface", "infrastructure", "bootstrap"):
        found = check_source(f"from {top}.x import y\n", layer="domain")
        assert len(found) == 1 and found[0].rule == "layer-dependency"


def test_application_forbids_interface_infrastructure_bootstrap() -> None:
    for top in ("interface", "infrastructure", "bootstrap"):
        found = check_source(f"from {top}.x import y\n", layer="application")
        assert len(found) == 1 and found[0].rule == "layer-dependency"


def test_langgraph_only_in_workflow_region() -> None:
    assert check_source("import langgraph\n", layer="application", workflow_region=True) == []
    found = check_source("import langgraph\n", layer="application")
    assert len(found) == 1 and found[0].rule == "third-party"
    assert "workflow" in found[0].message


def test_infrastructure_langgraph_checkpoint_exception() -> None:
    source = "from langgraph.checkpoint.base import BaseCheckpointSaver\n"
    assert check_source(source, layer="infrastructure") == []
    found = check_source("import langgraph\n", layer="infrastructure")
    assert len(found) == 1 and found[0].rule == "third-party"


def test_interface_only_calls_use_cases() -> None:
    assert check_source("import fastapi\n", layer="interface") == []
    assert check_source("from pydantic import BaseModel\n", layer="interface") == []
    for source in (
        "from domain.order.aggregate import Order\n",
        "from infrastructure.db import repo\n",
        "from bootstrap.api_main import create_app\n",
        "import sqlalchemy\n",
    ):
        found = check_source(source, layer="interface")
        assert len(found) == 1


def test_infrastructure_cannot_import_interface_or_bootstrap() -> None:
    found = check_source("from interface.http.routes import health\n", layer="infrastructure")
    assert len(found) == 1 and found[0].rule == "layer-dependency"
    found = check_source("from bootstrap.settings import RuntimeSettings\n", layer="infrastructure")
    assert len(found) == 1 and found[0].rule == "layer-dependency"


def test_migrations_importable_only_by_bootstrap() -> None:
    assert (
        check_source("from migrations.versions.v0001_baseline import VERSION\n", layer="bootstrap")
        == []
    )
    for layer in ("domain", "application", "interface", "infrastructure"):
        found = check_source("import migrations\n", layer=layer)
        assert len(found) == 1 and found[0].rule == "layer-dependency"


def test_infrastructure_allows_sdk_adapters() -> None:
    for source in (
        "import sqlalchemy\n",
        "import redis.asyncio\n",
        "from mcp.server.fastmcp import FastMCP\n",
        "from langchain_deepseek import ChatDeepSeek\n",
    ):
        assert check_source(source, layer="infrastructure") == []
    found = check_source("import fastapi\n", layer="infrastructure")
    assert len(found) == 1 and found[0].rule == "third-party"


def test_bootstrap_assembly_allowances() -> None:
    assert check_source("import uvicorn\n", layer="bootstrap") == []
    assert check_source("from pydantic_settings import BaseSettings\n", layer="bootstrap") == []
    found = check_source("import sqlalchemy\n", layer="bootstrap")
    assert len(found) == 1 and found[0].rule == "third-party"


def test_temporary_violation_is_detected_then_passes_after_revert(tmp_path: Path) -> None:
    fixtures = Path(__file__).resolve().parent / "fixtures"
    violation_template = (fixtures / "violation_domain_imports_orm.txt").read_text(encoding="utf-8")
    legal_template = (fixtures / "legal_workflow_imports_langgraph.txt").read_text(encoding="utf-8")
    (tmp_path / "src" / "domain").mkdir(parents=True)
    (tmp_path / "src" / "application" / "agent" / "workflow").mkdir(parents=True)
    (tmp_path / "bootstrap").mkdir()
    (tmp_path / "src" / "domain" / "__init__.py").write_text("import decimal\n", encoding="utf-8")
    (tmp_path / "src" / "application" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "application" / "agent" / "workflow" / "__init__.py").write_text(
        legal_template, encoding="utf-8"
    )
    (tmp_path / "bootstrap" / "__init__.py").write_text("import uvicorn\n", encoding="utf-8")
    assert scan_project(tmp_path) == []

    # 临时引入一项错误依赖：领域层导入 ORM，必须被检查发现。
    bad_file = tmp_path / "src" / "domain" / "bad.py"
    bad_file.write_text(violation_template, encoding="utf-8")
    violations = scan_project(tmp_path)
    assert len(violations) == 1
    assert violations[0].rule == "third-party"
    assert bad_file.name in str(violations[0].file)

    # 撤销后恢复通过。
    bad_file.unlink()
    assert scan_project(tmp_path) == []


def test_relative_import_resolves_to_layer(tmp_path: Path) -> None:
    (tmp_path / "src" / "domain").mkdir(parents=True)
    (tmp_path / "src" / "infrastructure").mkdir(parents=True)
    (tmp_path / "src" / "domain" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "infrastructure" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "domain" / "leak.py").write_text(
        "from ..infrastructure import x\n", encoding="utf-8"
    )
    (tmp_path / "src" / "infrastructure" / "inner.py").write_text(
        "from . import sibling\n", encoding="utf-8"
    )
    violations = {str(v.file): v.rule for v in scan_project(tmp_path)}
    assert violations.get("src\\domain\\leak.py") == "layer-dependency"
    assert "src\\infrastructure\\inner.py" not in violations


def test_unknown_top_level_package_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "src" / "domain").mkdir(parents=True)
    (tmp_path / "src" / "common").mkdir(parents=True)
    (tmp_path / "src" / "domain" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "common" / "utils.py").write_text("", encoding="utf-8")
    violations = scan_project(tmp_path)
    assert any(v.rule == "unknown-root" and "common" in str(v.file) for v in violations)
