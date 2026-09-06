"""Readiness estimation.

Two models run side by side. Which one drives scheduling is `PRIMARY_MODEL` —
one name, changed in one place (spec §6.3 exit criterion).
"""

from __future__ import annotations

from typing import Any

from dsa_coach.mechanism.readiness.base import Prediction, ReadinessModel, State
from dsa_coach.mechanism.readiness.baseline import (
    BaselineState,
    BetaBaselineReadinessModel,
    bucket_index,
)
from dsa_coach.mechanism.readiness.glicko import Glicko2ReadinessModel, GlickoState

#: Every model that runs and logs predictions, keyed by version.
MODELS: dict[str, ReadinessModel[Any]] = {
    BetaBaselineReadinessModel.version: BetaBaselineReadinessModel(),
    Glicko2ReadinessModel.version: Glicko2ReadinessModel(),
}

#: The model whose estimates the scheduler actually uses.
#:
#: The Beta baseline is primary until the Phase 6 bake-off says otherwise
#: (spec §6.3). Starting here means the sparse-data regime is handled correctly
#: by default rather than by assumption.
PRIMARY_MODEL: str = BetaBaselineReadinessModel.version


def primary() -> ReadinessModel[Any]:
    return MODELS[PRIMARY_MODEL]


__all__ = [
    "MODELS",
    "PRIMARY_MODEL",
    "BaselineState",
    "BetaBaselineReadinessModel",
    "Glicko2ReadinessModel",
    "GlickoState",
    "Prediction",
    "ReadinessModel",
    "State",
    "bucket_index",
    "primary",
]
