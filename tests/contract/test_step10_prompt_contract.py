"""步骤10 提示词契约：data/prompts/ 四段必须与 docs/Prompt设计.md 原文一致。

系统提示词、工具调用提示词、最终回答提示词按文档原文保持不变，不因实现
方便而改写或删减；本契约逐字节锁定文件与文档代码块的一致性。
"""

from __future__ import annotations

import re
from pathlib import Path

from application.agent.prompts import PROMPT_FILES, PromptCatalog

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROMPT_DOC = PROJECT_ROOT / "docs" / "Prompt设计.md"
PROMPTS_DIR = PROJECT_ROOT / "data" / "prompts"

# 文档中 ```text 代码块出现顺序：系统提示词、工具调用规则、节点段、最终回答。
EXPECTED_ORDER = ["system", "tool_rules", "supervisor_nodes", "final_answer"]


def _doc_blocks() -> list[str]:
    text = PROMPT_DOC.read_text(encoding="utf-8")
    blocks = re.findall(r"```text\n(.*?)\n```", text, flags=re.DOTALL)
    assert len(blocks) == len(EXPECTED_ORDER), (
        f"文档代码块数量变化: 期望 {len(EXPECTED_ORDER)}，实际 {len(blocks)}"
    )
    return [block.rstrip("\n") for block in blocks]


def test_prompt_files_match_document_verbatim() -> None:
    for key, expected in zip(EXPECTED_ORDER, _doc_blocks()):
        actual = (PROMPTS_DIR / PROMPT_FILES[key]).read_text(encoding="utf-8").rstrip("\n")
        assert actual == expected, f"{PROMPT_FILES[key]} 与 Prompt设计.md 原文不一致"


def test_prompt_catalog_loads_verbatim_texts() -> None:
    catalog = PromptCatalog.load(PROMPTS_DIR)
    for key, expected in zip(EXPECTED_ORDER, _doc_blocks()):
        assert getattr(catalog, key) == expected


def test_supervisor_system_prompt_is_verbatim_ordered_join() -> None:
    catalog = PromptCatalog.load(PROMPTS_DIR)
    assert catalog.supervisor_system_prompt() == "\n\n".join(
        (catalog.system, catalog.tool_rules, catalog.supervisor_nodes)
    )


def test_final_answer_prompt_is_verbatim_single_segment() -> None:
    catalog = PromptCatalog.load(PROMPTS_DIR)
    assert catalog.final_answer_system_prompt() == catalog.final_answer
