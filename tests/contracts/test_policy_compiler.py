from __future__ import annotations

from datetime import timedelta

import pytest

from trading.data.synthetic import (
    build_demo_scenario,
    build_news_fixture,
    build_policy_patch_fixture,
    source_record_for_scenario,
)
from trading.domain.contracts import PolicyOperation
from trading.domain.enums import PolicyAction, PolicyTargetKind
from trading.llm.policy_compiler import PolicyCompileError, PolicyCompiler, PolicyState


def policy_fixture():
    scenario = build_demo_scenario()
    source = source_record_for_scenario(scenario)
    news = build_news_fixture(scenario, source)
    return scenario, build_policy_patch_fixture(scenario, news)


def test_b3_risk_accepts_bounded_reduction_and_expires() -> None:
    scenario, patch = policy_fixture()
    compiler = PolicyCompiler()
    compiled = compiler.compile(
        patch,
        PolicyState.default("B3-RISK"),
        now=scenario.decision_time,
    )
    assert compiled.portfolio_risk_multiplier == 0.75
    assert compiled.version == 1

    restored = compiler.expire(
        compiled,
        now=patch.expires_at,
        expires_at=patch.expires_at,
    )
    assert restored.portfolio_risk_multiplier == 1.0
    assert restored.version == 2
    assert restored.source_patch_id is None


def test_explicit_restore_can_clear_an_active_total_bucket() -> None:
    scenario, patch = policy_fixture()
    compiler = PolicyCompiler()
    reduced = compiler.compile(
        patch,
        PolicyState.default("B3-RISK"),
        now=scenario.decision_time,
    )
    restore = PolicyOperation(
        action=PolicyAction.RESTORE_DEFAULT,
        target_kind=PolicyTargetKind.PORTFOLIO,
        target_id="TOTAL",
        risk_budget_delta=None,
        risk_multiplier=None,
        blocked=None,
    )
    restored = compiler.compile(
        patch.model_copy(
            update={
                "patch_id": "restore-patch",
                "base_policy_version": reduced.version,
                "operations": [restore],
            }
        ),
        reduced,
        now=scenario.decision_time,
    )

    assert restored.version == 2
    assert restored.portfolio_risk_multiplier == 1.0
    assert restored.active_buckets == frozenset()
    assert restored.source_patch_id == "restore-patch"


def test_b3_risk_rejects_positive_risk() -> None:
    scenario, patch = policy_fixture()
    positive = PolicyOperation(
        action=PolicyAction.REDUCE_RISK_BUDGET,
        target_kind=PolicyTargetKind.STRATEGY,
        target_id="T1",
        risk_budget_delta=0.10,
        risk_multiplier=None,
        blocked=None,
    )
    forbidden = patch.model_copy(update={"operations": [positive]})
    with pytest.raises(PolicyCompileError, match="must target PORTFOLIO:TOTAL"):
        PolicyCompiler().compile(
            forbidden,
            PolicyState.default("B3-RISK"),
            now=scenario.decision_time,
        )


def test_b3_risk_rejects_unconsumed_delta_and_symbol_block() -> None:
    scenario, patch = policy_fixture()
    delta_only = PolicyOperation(
        action=PolicyAction.REDUCE_RISK_BUDGET,
        target_kind=PolicyTargetKind.PORTFOLIO,
        target_id="TOTAL",
        risk_budget_delta=-0.10,
        risk_multiplier=None,
        blocked=None,
    )
    with pytest.raises(PolicyCompileError, match="requires risk_multiplier"):
        PolicyCompiler().compile(
            patch.model_copy(update={"operations": [delta_only]}),
            PolicyState.default("B3-RISK"),
            now=scenario.decision_time,
        )

    ignored_symbol = PolicyOperation(
        action=PolicyAction.BLOCK_NEW_ENTRIES,
        target_kind=PolicyTargetKind.SYMBOL,
        target_id="SPY",
        risk_budget_delta=None,
        risk_multiplier=None,
        blocked=True,
    )
    with pytest.raises(PolicyCompileError, match="target QQQ"):
        PolicyCompiler().compile(
            patch.model_copy(update={"operations": [ignored_symbol]}),
            PolicyState.default("B3-RISK"),
            now=scenario.decision_time,
        )


def test_base_policy_conflict_is_rejected() -> None:
    scenario, patch = policy_fixture()
    state = PolicyState(
        arm_id="B3-RISK",
        version=2,
        portfolio_risk_multiplier=1.0,
        strategy_risk_deltas={},
        blocked_targets=frozenset(),
        active_buckets=frozenset(),
        source_patch_id=None,
    )
    with pytest.raises(PolicyCompileError, match="version mismatch"):
        PolicyCompiler().compile(patch, state, now=scenario.decision_time)


def test_patch_before_effective_time_is_rejected() -> None:
    scenario, patch = policy_fixture()
    with pytest.raises(PolicyCompileError, match="not active"):
        PolicyCompiler().compile(
            patch,
            PolicyState.default("B3-RISK"),
            now=scenario.decision_time - timedelta(seconds=1),
        )
