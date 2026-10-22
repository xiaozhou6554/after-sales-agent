"""四层与模块依赖检查（步骤04）。

以 AST 解析 src/ 四层与 bootstrap/ 的全部 import 语句，对照《技术详细规格》
3.1/3.2 的依赖方向规则逐条判定：检查真实导入边界，不靠目录名字假定架构正确。

规则（技术详细规格 3.1、3.2）：

- Domain 不依赖其他三层，且为纯 Python 对象：禁止 Pydantic 传输模型、ORM、
  LangGraph、数据库驱动、HTTP/MCP Schema 等一切第三方库；
- Application 不导入 Interface/Infrastructure/bootstrap；LangGraph 是唯一框架
  依赖例外，且仅允许出现在 application/agent/workflow/ 编排区域；
- Interface 只调用用例：仅允许 fastapi/pydantic 传输 Schema，禁止导入 Domain、
  Infrastructure、bootstrap 及任何具体 SDK；
- Infrastructure 通过依赖注入实现内层端口：可导入 Domain 与 Application 暴露的
  端口，禁止导入 Interface/bootstrap；第三方仅放行自研 MySQLCheckpointSaver 所需
  的 langgraph.checkpoint 基类（技术详细规格 8.4）及各具体 SDK 适配器；
- bootstrap 仅负责配置、组装、启动：可导入四层、迁移包 migrations 与
  uvicorn/pydantic_settings/fastapi；migrations 为项目自有包，其余层禁止导入；
- 拒绝四层与 bootstrap 之外新建顶层包，防止以"公共工具类"绕过分层（端口必须
  显式定义在 application 层端口位置）。

运行方式：python tests/architecture/check_dependencies.py；发现违规时退出码为 1。
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCAN_ROOTS = (PROJECT_ROOT / "src", PROJECT_ROOT / "bootstrap")

LAYER_ROOTS = frozenset({"interface", "application", "domain", "infrastructure"})
LAYERS = frozenset({"interface", "application", "domain", "infrastructure", "bootstrap"})

# 项目自有但非四层的顶层包；仅组装根 bootstrap 允许导入。
FIRST_PARTY_EXTRA = frozenset({"migrations"})

# 层间禁止导入（技术详细规格 3.1：依赖方向固定；箭头只允许内层被外层依赖）。
FORBIDDEN_LAYERS: dict[str, frozenset[str]] = {
    "domain": frozenset({"application", "interface", "infrastructure", "bootstrap", "migrations"}),
    "application": frozenset({"interface", "infrastructure", "bootstrap", "migrations"}),
    "interface": frozenset({"domain", "infrastructure", "bootstrap", "migrations"}),
    "infrastructure": frozenset({"interface", "bootstrap", "migrations"}),
    "bootstrap": frozenset(),
}

# LangGraph 例外仅限应用编排区域 application/agent/workflow/（技术详细规格 3.2）。
WORKFLOW_EXTRA = frozenset({"langgraph", "langgraph_checkpoint"})

# 各层允许的第三方库。领域层无任何放行；应用层通用用例 DTO 保持框架无关。
THIRD_PARTY_ALLOW: dict[str, frozenset[str]] = {
    "domain": frozenset(),
    "application": frozenset(),
    # 传输 Schema 只存在于接口层。
    "interface": frozenset({"fastapi", "pydantic"}),
    # 具体驱动/框架适配器全部落在基础设施层。
    "infrastructure": frozenset(
        {
            "sqlalchemy",
            "pymysql",
            "redis",
            "pika",
            "neo4j",
            "chromadb",
            "rank_bm25",
            "jieba",
            "numpy",
            "huggingface_hub",
            "rdflib",
            "pyshacl",
            "langgraph_checkpoint",
            "langchain",
            "langchain_core",
            "langchain_deepseek",
            "langchain_huggingface",
            "langchain_mcp_adapters",
            "sentence_transformers",
            "mcp",
            "httpx",
            "cryptography",
            "jsonschema",
        }
    ),
    # 组装根只放行启动、配置与运维入口所需；pydantic 供配置模块做校验。
    "bootstrap": frozenset({"uvicorn", "pydantic_settings", "pydantic", "fastapi"}),
}


@dataclass(frozen=True)
class Violation:
    file: Path
    line: int
    rule: str
    message: str


def layer_of(path: Path, base: Path = PROJECT_ROOT) -> str | None:
    rel = path.relative_to(base)
    if rel.parts[0] == "bootstrap":
        return "bootstrap"
    if rel.parts[0] == "src" and len(rel.parts) > 1 and rel.parts[1] in LAYER_ROOTS:
        return rel.parts[1]
    return None


def module_name_of(path: Path, base: Path = PROJECT_ROOT) -> str:
    rel = path.relative_to(base).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[0] == "src":
        # 源码位于 src/ 下，但安装后以顶层包名导入，相对导入须按安装名解析。
        parts = parts[1:]
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def in_workflow_region(path: Path, base: Path = PROJECT_ROOT) -> bool:
    """是否位于 application/agent/workflow/ 编排区域（__init__ 同样按区域判定）。"""
    app_root = base / "src" / "application"
    try:
        rel = path.relative_to(app_root)
    except ValueError:
        return False
    return rel.parts[:2] == ("agent", "workflow")


def normalize_third_party(parts: list[str]) -> str:
    """pip 发行版 langgraph-checkpoint 的导入根是 langgraph.checkpoint，归一为键名。"""
    if parts[0] == "langgraph" and len(parts) > 1 and parts[1] == "checkpoint":
        return "langgraph_checkpoint"
    return parts[0]


def collect_imports(
    tree: ast.Module, module: str, is_package: bool = False
) -> list[tuple[str, int]]:
    """返回 (导入的完整模块路径, 行号)；相对导入按文件所在包解析为绝对路径。

    ``module`` 为模块完整路径（如 domain.leak）或包路径（__init__，如 domain）；
    level=1 指当前包，因此普通模块先去掉末段模块名再按层级上溯。
    """
    found: list[tuple[str, int]] = []
    base = module.split(".") if module else []
    package_depth = len(base) if is_package else max(len(base) - 1, 0)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module:
                    found.append((node.module, node.lineno))
            else:
                keep = package_depth - (node.level - 1)
                pkg = base[:keep] if keep > 0 else []
                segments = [*pkg, node.module] if node.module else pkg
                if segments:
                    found.append((".".join(segments), node.lineno))
    return found


def check_source(
    source: str,
    *,
    layer: str,
    workflow_region: bool = False,
    module: str = "",
) -> list[Violation]:
    """按层规则判定一段源码的全部 import；layer 为 None 交由 scan_project 处理。"""
    violations: list[Violation] = []
    tree = ast.parse(source)
    allowed = set(THIRD_PARTY_ALLOW[layer])
    if workflow_region:
        allowed |= WORKFLOW_EXTRA
    for imported, lineno in collect_imports(tree, module):
        parts = imported.split(".")
        top = parts[0]
        if top in LAYERS or top in FIRST_PARTY_EXTRA:
            if top in FORBIDDEN_LAYERS[layer]:
                rule = "layer-dependency"
                message = f"{layer} 层禁止导入 {top}（{imported}）"
            else:
                continue
        elif top in sys.stdlib_module_names:
            continue
        else:
            key = normalize_third_party(parts)
            if key in allowed:
                continue
            rule = "third-party"
            message = f"{layer} 层导入未被允许的第三方库 {imported}"
            if layer == "application" and key in WORKFLOW_EXTRA:
                message += "（LangGraph 仅限 application/agent/workflow/ 编排区域）"
        violations.append(
            Violation(
                file=Path(module.replace(".", "/") or "<source>"),
                line=lineno,
                rule=rule,
                message=message,
            )
        )
    return violations


def scan_project(root: Path | None = None) -> list[Violation]:
    """扫描 src/ 四层与 bootstrap/ 全部 Python 文件并返回违规列表。"""
    base = root or PROJECT_ROOT
    violations: list[Violation] = []
    scan_dirs = [base / "src", base / "bootstrap"] if root else list(SCAN_ROOTS)
    for scan_dir in scan_dirs:
        if not scan_dir.exists():
            continue
        for path in sorted(scan_dir.rglob("*.py")):
            layer = layer_of(path, base)
            if layer is None:
                violations.append(
                    Violation(
                        file=path.relative_to(base),
                        line=1,
                        rule="unknown-root",
                        message=f"存在四层与 bootstrap 之外的顶层包：{path.relative_to(base)}",
                    )
                )
                continue
            try:
                source = path.read_text(encoding="utf-8")
                tree = ast.parse(source)
            except (SyntaxError, UnicodeDecodeError) as exc:
                violations.append(
                    Violation(
                        file=path.relative_to(base),
                        line=getattr(exc, "lineno", None) or 1,
                        rule="syntax-error",
                        message=f"无法解析 import：{exc}",
                    )
                )
                continue
            for imported, lineno in collect_imports(
                tree, module_name_of(path, base), is_package=path.name == "__init__.py"
            ):
                parts = imported.split(".")
                top = parts[0]
                if top in LAYERS or top in FIRST_PARTY_EXTRA:
                    if top in FORBIDDEN_LAYERS[layer]:
                        violations.append(
                            Violation(
                                file=path.relative_to(base),
                                line=lineno,
                                rule="layer-dependency",
                                message=f"{layer} 层禁止导入 {top}（{imported}）",
                            )
                        )
                    continue
                if top in sys.stdlib_module_names:
                    continue
                key = normalize_third_party(parts)
                allowed = set(THIRD_PARTY_ALLOW[layer])
                if in_workflow_region(path, base):
                    allowed |= WORKFLOW_EXTRA
                if key in allowed:
                    continue
                message = f"{layer} 层导入未被允许的第三方库 {imported}"
                if layer == "application" and key in WORKFLOW_EXTRA:
                    message += "（LangGraph 仅限 application/agent/workflow/ 编排区域）"
                violations.append(
                    Violation(
                        file=path.relative_to(base),
                        line=lineno,
                        rule="third-party",
                        message=message,
                    )
                )
    return violations


def main() -> int:
    violations = scan_project()
    file_count = sum(1 for scan_dir in SCAN_ROOTS for _ in scan_dir.rglob("*.py"))
    if violations:
        print(f"依赖检查发现 {len(violations)} 处违规：")
        for item in violations:
            print(f"  [{item.rule}] {item.file}:{item.line} {item.message}")
        return 1
    print(f"依赖检查通过：共检查 {file_count} 个文件，无违规导入。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
