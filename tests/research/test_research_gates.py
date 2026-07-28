from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trading.research.contracts import FalsificationStatus, PromotionVerdict
from trading.research.falsification import (
    MANDATORY_FALSIFICATION_TESTS,
    ExperimentBudget,
    build_falsification_report,
    make_test_result,
)
from trading.research.oos_lockbox import (
    OosEvaluationRequest,
    OosLockboxService,
    PrivateOosObservation,
)
from trading.research.promotion import (
    REQUIRED_PROMOTION_CRITERIA,
    evaluate_promotion_eligibility,
)
from trading.research.sandbox_contract import (
    CandidatePatchRejected,
    inspect_candidate_patch,
)

NOW = datetime(2026, 7, 27, 20, 0, tzinfo=UTC)


def _results(
    *,
    failing: str | None = None,
    budget_status: FalsificationStatus = FalsificationStatus.PASS,
):
    return [
        make_test_result(
            test_id=test_id,
            status=(
                budget_status
                if test_id == "experiment_budget"
                else (
                    FalsificationStatus.FAIL
                    if test_id == failing
                    else FalsificationStatus.PASS
                )
            ),
            reason_code="CHECKED",
            metrics={"synthetic": True},
        )
        for test_id in MANDATORY_FALSIFICATION_TESTS
    ]


def test_mandatory_failure_prevents_shadow() -> None:
    report = build_falsification_report(
        challenger_id="challenger-1",
        results=_results(failing="date_shift_placebo"),
        budget=ExperimentBudget("family", 0, 2, 0, 1),
        created_at=NOW,
    )
    assert report.mandatory_passed is False


def test_experiment_budget_is_authoritative() -> None:
    with pytest.raises(ValueError, match="budget"):
        build_falsification_report(
            challenger_id="challenger-1",
            results=_results(),
            budget=ExperimentBudget("family", 2, 2, 0, 1),
            created_at=NOW,
        )


def test_candidate_patch_path_jail() -> None:
    patch = (
        b"diff --git a/src/trading/strategies/challengers/t2_v1.py "
        b"b/src/trading/strategies/challengers/t2_v1.py\n"
        b"diff --git a/tests/research/test_t2.py b/tests/research/test_t2.py\n"
    )
    accepted = inspect_candidate_patch(
        changed_paths=[
            "src/trading/strategies/challengers/t2_v1.py",
            "tests/research/test_t2.py",
        ],
        patch_bytes=patch,
    )
    assert len(accepted.patch_hash) == 64
    with pytest.raises(CandidatePatchRejected, match="FORBIDDEN_PATH"):
        inspect_candidate_patch(
            changed_paths=["src/trading/risk/state_machine.py"],
            patch_bytes=(
                b"diff --git a/src/trading/risk/state_machine.py "
                b"b/src/trading/risk/state_machine.py\n"
            ),
        )
    with pytest.raises(CandidatePatchRejected, match="CHAMPION_IN_PLACE_CHANGE"):
        inspect_candidate_patch(
            changed_paths=["src/trading/strategies/t1.py"],
            patch_bytes=(
                b"diff --git a/src/trading/strategies/t1.py "
                b"b/src/trading/strategies/t1.py\n"
            ),
            champion_owned_paths=["src/trading/strategies/t1.py"],
        )


def test_candidate_patch_rejects_declared_path_mismatch() -> None:
    patch = (
        b"diff --git a/src/trading/risk/state_machine.py "
        b"b/src/trading/risk/state_machine.py\n"
    )
    with pytest.raises(
        CandidatePatchRejected,
        match="DECLARED_PATHS_DO_NOT_MATCH_PATCH",
    ):
        inspect_candidate_patch(
            changed_paths=["tests/research/test_safe.py"],
            patch_bytes=patch,
        )


class _Budget:
    def reserve(self, *, experiment_family: str, submission_number: int) -> int:
        assert experiment_family == "family"
        assert submission_number == 1
        return 1


class _Evaluator:
    def evaluate(self, request):
        del request
        return [
            PrivateOosObservation("private-a", 0.02, 0.01),
            PrivateOosObservation("private-b", -0.01, -0.02),
        ]


def test_oos_lockbox_returns_aggregate_only() -> None:
    service = OosLockboxService(
        evaluator=_Evaluator(),
        budget_ledger=_Budget(),
        minimum_common_sessions=2,
        minimum_mean_daily_difference=0.005,
    )
    result = service.evaluate(
        OosEvaluationRequest(
            challenger_id="challenger-1",
            experiment_family="family",
            submission_number=1,
            candidate_artifact_hash="a" * 64,
            evaluation_contract_hash="b" * 64,
        ),
        evaluated_at=NOW,
    )
    payload = result.model_dump(mode="json")
    assert result.verdict.value == "PASS"
    assert "private-a" not in str(payload)
    assert set(result.aggregate_statistics) == {"mean_daily_difference"}


def test_promotion_stops_at_manual_eligibility() -> None:
    criteria = {name: True for name in REQUIRED_PROMOTION_CRITERIA}
    result = evaluate_promotion_eligibility(
        promotion_decision_id="promotion-1",
        challenger_id="challenger-1",
        current_champion_version="1.0.0",
        candidate_version="2.0.0",
        criteria=criteria,
        replay_hash="c" * 64,
        created_at=NOW,
    )
    assert result.verdict is PromotionVerdict.ELIGIBLE_REQUIRES_MANUAL_APPROVAL
    assert result.automatic_promotion_enabled is False
    assert result.approved_by is None
