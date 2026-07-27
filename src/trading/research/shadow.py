from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ShadowExecutionContract:
    market_input_manifest_hash: str
    decision_schedule_version: str
    execution_scenario_version: str
    cost_model_version: str
    starting_capital_usd: str
    liquidity_policy_version: str


@dataclass(frozen=True, slots=True)
class ShadowArmIdentity:
    arm_id: str
    strategy_id: str
    strategy_version: str
    contract: ShadowExecutionContract


def require_matched_shadow_contract(
    champion: ShadowArmIdentity,
    challenger: ShadowArmIdentity,
) -> None:
    if champion.arm_id == challenger.arm_id:
        raise ValueError("Champion and Challenger need independent shadow arms")
    if champion.strategy_version == challenger.strategy_version:
        raise ValueError("Challenger cannot reuse Champion strategy version")
    if champion.contract != challenger.contract:
        raise ValueError("matched shadow arms must share execution conditions")


@dataclass(frozen=True, slots=True)
class FactorialAttribution:
    guard_main_effect: float
    ai_main_effect: float
    ai_guard_interaction_effect: float
    common_sessions: int


def calculate_ai_guard_factorial(
    *,
    b0_vol: list[float],
    b3_guard: list[float],
    b3_ai: list[float],
    b3_ai_guard: list[float],
) -> FactorialAttribution:
    lengths = {len(b0_vol), len(b3_guard), len(b3_ai), len(b3_ai_guard)}
    if len(lengths) != 1:
        raise ValueError("2x2 attribution requires common matched sessions")
    count = lengths.pop()
    if count == 0:
        raise ValueError("2x2 attribution requires at least one session")
    guard_effects = [
        0.5 * ((guard - base) + (both - ai))
        for base, guard, ai, both in zip(
            b0_vol,
            b3_guard,
            b3_ai,
            b3_ai_guard,
            strict=True,
        )
    ]
    ai_effects = [
        0.5 * ((ai - base) + (both - guard))
        for base, guard, ai, both in zip(
            b0_vol,
            b3_guard,
            b3_ai,
            b3_ai_guard,
            strict=True,
        )
    ]
    interactions = [
        both - ai - guard + base
        for base, guard, ai, both in zip(
            b0_vol,
            b3_guard,
            b3_ai,
            b3_ai_guard,
            strict=True,
        )
    ]
    return FactorialAttribution(
        guard_main_effect=sum(guard_effects) / count,
        ai_main_effect=sum(ai_effects) / count,
        ai_guard_interaction_effect=sum(interactions) / count,
        common_sessions=count,
    )
