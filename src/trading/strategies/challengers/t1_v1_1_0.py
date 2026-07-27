from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trading.domain.time import require_aware_utc

STRATEGY_ID = "T1"
STRATEGY_VERSION = "1.1.0"
PARENT_VERSION = "1.0.0"
HYPOTHESIS_ID = "T1_BREADTH_DEFINITION_REVISION_V1"


@dataclass(frozen=True, slots=True)
class BreadthMemberObservation:
    symbol: str
    universe_id: str
    membership_effective: bool
    above_slow_average: bool
    positive_intermediate_return: bool
    available_at: datetime

    def __post_init__(self) -> None:
        require_aware_utc(self.available_at)


@dataclass(frozen=True, slots=True)
class BreadthRevisionResult:
    breadth_score: Decimal
    eligible_members: int
    expected_members: int
    coverage: Decimal


def revised_equal_weight_breadth(
    observations: list[BreadthMemberObservation],
    *,
    universe_id: str,
    expected_members: int,
    data_available_cutoff: datetime,
    minimum_coverage: Decimal,
) -> BreadthRevisionResult:
    """Candidate-only PIT breadth definition.

    The revision replaces the parent feature's four-indicator average with two
    interpretable, equally weighted conditions. It deliberately remains a
    Challenger helper and is not imported by the Champion.
    """

    cutoff = require_aware_utc(data_available_cutoff)
    if expected_members <= 0:
        raise ValueError("expected_members must be positive")
    if not Decimal("0") < minimum_coverage <= Decimal("1"):
        raise ValueError("minimum_coverage must be in (0, 1]")
    eligible = [
        item
        for item in observations
        if item.universe_id == universe_id
        and item.membership_effective
        and item.available_at <= cutoff
    ]
    symbols = [item.symbol for item in eligible]
    if len(symbols) != len(set(symbols)):
        raise ValueError("duplicate point-in-time member observation")
    coverage = Decimal(len(eligible)) / Decimal(expected_members)
    if coverage < minimum_coverage:
        raise ValueError("PIT_CONSTITUENT_COVERAGE_BELOW_THRESHOLD")
    positive_conditions = sum(
        int(item.above_slow_average) + int(item.positive_intermediate_return)
        for item in eligible
    )
    score = Decimal(positive_conditions) / Decimal(len(eligible) * 2)
    return BreadthRevisionResult(
        breadth_score=score,
        eligible_members=len(eligible),
        expected_members=expected_members,
        coverage=coverage,
    )
