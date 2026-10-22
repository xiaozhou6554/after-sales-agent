"""长期记忆档案的租户、客户一致性边界。"""

from __future__ import annotations

from dataclasses import dataclass, field

from domain.memory.entities.memory_fact import MemoryFact


@dataclass
class MemoryProfile:
    tenant_id: str
    user_id: str
    facts: list[MemoryFact] = field(default_factory=list)

    def remember(self, fact: MemoryFact) -> None:
        for index, current in enumerate(self.facts):
            if (current.fact_type, current.fact_key) == (fact.fact_type, fact.fact_key):
                self.facts[index] = fact
                return
        self.facts.append(fact)
