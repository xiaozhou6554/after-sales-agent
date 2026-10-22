"""调查用例的存储端口委托。SQL 查询与修订持久化由 Infrastructure 提供。"""

from __future__ import annotations

from typing import Protocol


class InvestigationPort(Protocol):
    def __getattr__(self, name: str): ...


class InvestigationService:
    def __init__(self, investigation: InvestigationPort) -> None:
        self._investigation = investigation

    def __getattr__(self, name: str):
        return getattr(self._investigation, name)
