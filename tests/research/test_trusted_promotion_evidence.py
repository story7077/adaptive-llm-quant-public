from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from trading.research.contracts import PromotionVerdict
from trading.research.promotion_evidence import (
    PromotionEvaluationContractV1,
    build_promotion_evidence,
    evaluate_trusted_promotion_evidence,
)

NOW = datetime(2026, 7, 27, 21, 0, tzinfo=UTC)


def _contract() -> PromotionEvaluationContractV1:
    return PromotionEvaluationContractV1(
        contract_version="promotion-contract-v1",
        minimum_common_oos_sessions=126,
        minimum_forward_sessions=63,
        minimum_independent_trades=30,
        minimum_annualized_net_excess_return_after_cost=0.0,
        minimum_matched_annualized_difference=0.0,
        minimum_economic_effect=0.01,
        maximum_drawdown=0.20,
        maximum_tail_loss=0.05,
        maximum_annualized_turnover=12.0,
        minimum_capacity_usd=100_000.0,
        minimum_regime_pass_fraction=0.67,
        maximum_runtime_error_rate=0.01,
    )


def _evidence(**updates: object):
    values: dict[str, object] = {
        "evidence_id": "promotion-evidence-1",
        "challenger_id": "challenger-1",
        "current_champion_version": "1.0.0",
        "candidate_version": "1.1.0",
        "candidate_artifact_hash": "a" * 64,
        "falsification_report_hash": "b" * 64,
        "oos_result_hash": "c" * 64,
        "shadow_summary_hash": "d" * 64,
        "replay_hash": "e" * 64,
        "common_oos_sessions": 126,
        "forward_sessions": 63,
        "independent_trades": 30,
        "annualized_net_excess_return_after_cost": 0.02,
        "matched_annualized_difference": 0.015,
        "economic_effect": 0.01,
        "maximum_drawdown": 0.10,
        "tail_loss": 0.03,
        "annualized_turnover": 6.0,
        "estimated_capacity_usd": 1_000_000.0,
        "regime_pass_fraction": 0.75,
        "runtime_error_rate": 0.0,
        "replay_reproducible": True,
        "mandatory_tests_passed": True,
        "data_available_cutoff": NOW - timedelta(minutes=1),
        "created_at": NOW,
    }
    values.update(updates)
    return build_promotion_evidence(**values)  # type: ignore[arg-type]


def test_trusted_metrics_produce_eligibility_but_never_auto_promotion() -> None:
    first = evaluate_trusted_promotion_evidence(
        evidence=_evidence(),
        contract=_contract(),
        created_at=NOW,
    )
    second = evaluate_trusted_promotion_evidence(
        evidence=_evidence(),
        contract=_contract(),
        created_at=NOW,
    )

    assert first.evaluation_hash == second.evaluation_hash
    assert first.decision.verdict is (
        PromotionVerdict.ELIGIBLE_REQUIRES_MANUAL_APPROVAL
    )
    assert first.decision.automatic_promotion_enabled is False
    assert first.decision.approved_by is None


@pytest.mark.parametrize(
    ("field", "value", "failed_criterion"),
    [
        ("common_oos_sessions", 125, "minimum_forward_period"),
        ("forward_sessions", 62, "minimum_forward_period"),
        ("independent_trades", 29, "minimum_independent_trades"),
        (
            "annualized_net_excess_return_after_cost",
            -0.01,
            "net_excess_return_after_cost",
        ),
        (
            "matched_annualized_difference",
            -0.01,
            "matched_baseline_improvement",
        ),
        ("economic_effect", 0.009, "minimum_economic_effect"),
        ("maximum_drawdown", 0.21, "maximum_drawdown"),
        ("tail_loss", 0.06, "tail_risk"),
        ("annualized_turnover", 12.1, "turnover"),
        ("estimated_capacity_usd", 99_999.0, "capacity"),
        ("regime_pass_fraction", 0.66, "regime_robustness"),
        ("runtime_error_rate", 0.02, "error_rate"),
        ("replay_reproducible", False, "replay_reproducible"),
        ("mandatory_tests_passed", False, "mandatory_tests"),
    ],
)
def test_each_metric_can_fail_its_predeclared_gate(
    field: str,
    value: object,
    failed_criterion: str,
) -> None:
    result = evaluate_trusted_promotion_evidence(
        evidence=_evidence(**{field: value}),
        contract=_contract(),
        created_at=NOW,
    )

    assert result.decision.verdict is PromotionVerdict.INELIGIBLE
    assert result.decision.criteria[failed_criterion] is False
    assert failed_criterion.upper() in result.decision.failed_reason_codes


def test_evidence_is_hash_bound_and_contains_no_model_confidence() -> None:
    evidence = _evidence()
    assert "confidence" not in evidence.model_dump(mode="python")
    payload = evidence.model_dump(mode="python")
    payload["forward_sessions"] = 64
    with pytest.raises(ValidationError, match="hash mismatch"):
        type(evidence).model_validate(payload)
