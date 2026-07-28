from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trading.research.contracts import ChallengerStatus
from trading.research.lifecycle import (
    ResearchLifecycleError,
    ResearchLifecycleService,
)
from trading.research.oos_lockbox import OosEvaluationRequest
from trading.research.shadow import ShadowArmIdentity, ShadowExecutionContract

NOW = datetime(2026, 7, 27, 20, 0, tzinfo=UTC)


class _RepositoryWithoutPassedReport:
    def oos_result(self, **_: object) -> None:
        return None

    def has_passed_falsification(self, **_: object) -> bool:
        return False

    def challenger_status(self, _: str) -> ChallengerStatus:
        return ChallengerStatus.PROPOSED


class _CountingOosLockbox:
    calls = 0

    def evaluate(self, *_: object, **__: object) -> None:
        self.calls += 1
        raise AssertionError("lockbox must not run before falsification passes")


def _shadow_pair() -> tuple[ShadowArmIdentity, ShadowArmIdentity]:
    contract = ShadowExecutionContract(
        market_input_manifest_hash="a" * 64,
        decision_schedule_version="schedule-v1",
        execution_scenario_version="paper-conservative-v1",
        cost_model_version="cost-v1",
        starting_capital_usd="100000.00",
        liquidity_policy_version="liquidity-v1",
    )
    return (
        ShadowArmIdentity("champion-arm", "T1", "1.0.0", contract),
        ShadowArmIdentity("challenger-arm", "T1", "1.1.0", contract),
    )


def test_oos_is_not_invoked_before_falsification_passes() -> None:
    lockbox = _CountingOosLockbox()
    service = ResearchLifecycleService(
        repository=_RepositoryWithoutPassedReport(),  # type: ignore[arg-type]
        oos_lockbox=lockbox,  # type: ignore[arg-type]
    )
    champion, challenger = _shadow_pair()

    with pytest.raises(
        ResearchLifecycleError,
        match="cannot run before mandatory falsification",
    ):
        service.evaluate_oos_and_register_shadow(
            OosEvaluationRequest(
                challenger_id="challenger-1",
                experiment_family="family-1",
                submission_number=1,
                candidate_artifact_hash="b" * 64,
                evaluation_contract_hash="c" * 64,
            ),
            champion_shadow=champion,
            challenger_shadow=challenger,
            evaluated_at=NOW,
            persisted_at=NOW,
        )

    assert lockbox.calls == 0
