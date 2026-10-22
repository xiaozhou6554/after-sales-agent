"""长期事实：只保存有后续售后价值的稳定信息。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import exp


@dataclass(frozen=True)
class MemoryFact:
    fact_type: str
    fact_key: str
    fact_value: str
    importance: float
    source: str
    updated_at: datetime

    def score_at(self, now: datetime) -> float:
        if self.importance >= 0.8:
            return self.importance
        age_days = max(
            0.0,
            (
                now.astimezone(timezone.utc) - self.updated_at.astimezone(timezone.utc)
            ).total_seconds()
            / 86400,
        )
        return self.importance * exp(-age_days / 180.0)

    def cleanup_candidate(self, now: datetime) -> bool:
        return self.score_at(now) < 0.2
