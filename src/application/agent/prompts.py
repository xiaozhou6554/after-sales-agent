"""提示词目录加载：原文加载 data/prompts/ 下的四段提示词，不拼装不改写。

文件内容必须与 docs/Prompt设计.md 的对应代码块逐字节一致（契约测试锁定）；
本模块只负责读取与拼接说明位置，不修改任何提示词文字。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PROMPT_FILES = {
    "system": "system.md",
    "tool_rules": "tool_rules.md",
    "supervisor_nodes": "supervisor_nodes.md",
    "final_answer": "final_answer.md",
}


@dataclass(frozen=True)
class PromptCatalog:
    """四段提示词的只读目录；内容由契约测试保证与文档一致。"""

    system: str
    tool_rules: str
    supervisor_nodes: str
    final_answer: str

    @classmethod
    def load(cls, prompts_dir: str | Path) -> "PromptCatalog":
        directory = Path(prompts_dir)
        texts = {}
        for key, filename in PROMPT_FILES.items():
            path = directory / filename
            if not path.is_file():
                raise FileNotFoundError(f"提示词文件缺失: {path}")
            texts[key] = path.read_text(encoding="utf-8").rstrip("\n")
        return cls(**texts)

    def supervisor_system_prompt(self) -> str:
        """Supervisor 节点系统提示词：系统提示词＋工具调用规则＋节点段（原文顺序拼接）。"""
        return "\n\n".join((self.system, self.tool_rules, self.supervisor_nodes))

    def final_answer_system_prompt(self) -> str:
        """最终回答节点系统提示词（6.4 原文）。"""
        return self.final_answer
