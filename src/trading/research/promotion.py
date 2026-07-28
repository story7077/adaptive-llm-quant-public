from __future__ import annotations

from datetime import datetime

from trading.domain.hashing import canonical_hash
from trading.domain.time import require_aware_utc
from trading.research.contracts import PromotionDecisionV1, PromotionVerdict

REQUIRED_PROMOTION_CRITERIA = (
    "minimum_independent_trades",
    "minimum_forward_period",
    "net_excess_return_after_cost",
    "matched_baseline_improvement",
    "minimum_economic_effect",
    "maximum_drawdown",
    "tail_risk",
    "turnover",
    "capacity",
    "regime_robustness",
    "error_rate",
    "replay_reproducible",
    "mandatory_tests",
)


def evaluate_promotion_eligibility(
    *,
    promotion_decision_id: str,
    challenger_id: str,
    current_champion_version: str,
    candidate_version: str,
    criteria: dict[str, bool],
    replay_hash: str,
    created_at: datetime,
) -> PromotionDecisionV1:
    missing = sorted(set(REQUIRED_PROMOTION_CRITERIA) - set(criteria))
    unknown = sorted(set(criteria) - set(REQUIRED_PROMOTION_CRITERIA))
    if missing or unknown:
        raise ValueError(
            f"promotion criteria mismatch missing={missing} unknown={unknown}"
        )
    failed = [name.upper() for name in REQUIRED_PROMOTION_CRITERIA if not criteria[name]]
    verdict = (
        PromotionVerdict.ELIGIBLE_REQUIRES_MANUAL_APPROVAL
        if not failed
        else PromotionVerdict.INELIGIBLE
    )
    timestamp = require_aware_utc(created_at)
    payload = {
        "schema_version": "promotion_decision_v1",
        "promotion_decision_id": promotion_decision_id,
        "challenger_id": challenger_id,
        "current_champion_version": current_champion_version,
        "candidate_version": candidate_version,
        "verdict": verdict,
        "criteria": criteria,
        "failed_reason_codes": failed,
        "replay_hash": replay_hash,
        "automatic_promotion_enabled": False,
        "approved_by": None,
        "created_at": timestamp,
    }
    return PromotionDecisionV1.model_validate(
        {
            **payload,
            "decision_hash": canonical_hash(payload),
        }
    )
