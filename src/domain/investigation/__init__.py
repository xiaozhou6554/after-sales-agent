"""因素贡献与可比性门（FR-24）。"""

from domain.investigation.contribution import (
    Contribution,
    FactorGroup,
    comparability_gate,
    factor_contributions,
    percentage_points,
)

__all__ = [
    "Contribution",
    "FactorGroup",
    "comparability_gate",
    "factor_contributions",
    "percentage_points",
]
