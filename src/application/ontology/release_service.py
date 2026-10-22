"""本体发布用例的存储与校验端口委托。"""

from __future__ import annotations

from typing import Protocol


class OntologyReleasePort(Protocol):
    def __getattr__(self, name: str): ...


class OntologyReleaseService:
    def __init__(self, release: OntologyReleasePort) -> None:
        self._release = release

    def __getattr__(self, name: str):
        return getattr(self._release, name)
