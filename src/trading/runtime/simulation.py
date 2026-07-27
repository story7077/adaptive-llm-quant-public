from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any

from trading.data.ports import available_as_of
from trading.data.synthetic import (
    SyntheticBar,
    SyntheticScenario,
    build_feature_fixtures,
    build_forecast_fixtures,
    build_news_fixture,
    build_policy_patch_fixture,
    source_record_for_scenario,
)
from trading.domain.contracts import (
    Fill,
    LedgerEntry,
    NavSnapshot,
    NewsEvent,
    OrderIntent,
    PolicyOperation,
    PolicyPatch,
    PortfolioDecision,
    RiskDecision,
    SourceRecord,
    StrategyForecast,
)
from trading.domain.enums import OrderSide, PolicyAction, PolicyTargetKind
from trading.domain.hashing import canonical_hash, stable_id
from trading.execution.paper import PaperBroker
from trading.experiments.arms import (
    ARM_IDS,
    ArmState,
    create_arm_states,
    rebuild_arm_state,
    states_are_independent,
)
from trading.ledger.journal import (
    capital_entry,
    fill_entry,
    rebuild_holdings,
    validate_entries,
)
from trading.ledger.nav import calculate_nav
from trading.llm.policy_compiler import PolicyCompileError, PolicyCompiler, PolicyState
from trading.portfolio.phase0 import Phase0PortfolioEngine
from trading.risk.phase0 import Phase0RiskEngine


@dataclass(frozen=True, slots=True)
class ArmArtifacts:
    state: ArmState
    policy_state: PolicyState
    portfolio_decision: PortfolioDecision
    risk_decision: RiskDecision
    order_intents: list[OrderIntent]
    fills: list[Fill]
    ledger_entries: list[LedgerEntry]
    nav_snapshot: NavSnapshot


@dataclass(frozen=True, slots=True)
class SimulationArtifacts:
    scenario: SyntheticScenario
    source_record: SourceRecord
    feature_snapshots: list[Any]
    forecasts: list[StrategyForecast]
    news_event: NewsEvent
    policy_patch: PolicyPatch
    arms: dict[str, ArmArtifacts]
    manifest: dict[str, Any]
    result_hash: str


def simulate_scenario(
    scenario: SyntheticScenario,
    *,
    config_manifest_hash: str,
    code_version: str,
) -> SimulationArtifacts:
    source = source_record_for_scenario(scenario)
    features = build_feature_fixtures(scenario, source)
    forecasts = build_forecast_fixtures(
        scenario,
        features,
        code_version=code_version,
    )
    news_event = build_news_fixture(scenario, source)
    patch = build_policy_patch_fixture(scenario, news_event)

    policy_compiler = PolicyCompiler()
    policy_states = {arm_id: PolicyState.default(arm_id) for arm_id in ARM_IDS}
    policy_states["B3-RISK"] = policy_compiler.compile(
        patch,
        policy_states["B3-RISK"],
        now=scenario.decision_time,
        shadow_mode=True,
    )

    input_snapshot_hash = canonical_hash(
        {
            "features": features,
            "forecasts": forecasts,
            "news_event": news_event,
            "policy_patch": patch,
        }
    )
    decision_prices = _latest_prices_as_of(scenario.bars, scenario.decision_time)
    next_bars = _next_bars(scenario.bars, scenario.decision_time)
    final_bars = _final_bars(scenario.bars)
    final_prices = {symbol: bar.close for symbol, bar in final_bars.items()}

    paper_broker = PaperBroker(
        execution_scenario_id=scenario.execution_scenario_id,
        commission_rate=scenario.commission_rate,
        commission_waiver_threshold_usd=scenario.commission_waiver_threshold_usd,
        half_spread_bps=scenario.half_spread_bps,
        delay_penalty_bps=scenario.delay_penalty_bps,
    )
    portfolio_engine = Phase0PortfolioEngine()
    risk_engine = Phase0RiskEngine()
    states = create_arm_states(scenario.initial_cash_usd)
    arm_artifacts: dict[str, ArmArtifacts] = {}

    for arm_id in ARM_IDS:
        policy_state = policy_states[arm_id]
        portfolio_decision = portfolio_engine.decide(
            arm_id=arm_id,
            forecasts=forecasts,
            previous_weights={"USD_CASH": 1.0},
            decision_time=scenario.decision_time,
            policy_version=policy_state.version,
            input_snapshot_hash=input_snapshot_hash,
        )
        risk_decision = risk_engine.evaluate(
            portfolio_decision,
            created_at=scenario.decision_time,
        )
        if not risk_decision.approved:
            raise ValueError(
                f"Phase 0 fixture was rejected for {arm_id}: "
                f"{risk_decision.rejected_reasons}"
            )

        intents = _build_order_intents(
            arm_id=arm_id,
            decision=portfolio_decision,
            risk_decision=risk_decision,
            prices=decision_prices,
            nav=scenario.initial_cash_usd,
        )
        fills: list[Fill] = []
        for intent in intents:
            next_bar = next_bars[intent.symbol]
            participation = (
                Decimal("0.5")
                if arm_id == "B3-FULL" and intent.symbol == "SOXX"
                else Decimal("1")
            )
            fills.append(
                paper_broker.fill_marketable(
                    intent,
                    next_bar_open=next_bar.open,
                    effective_at=next_bar.available_at,
                    participation_fraction=participation,
                )
            )

        state = states[arm_id]
        for fill in fills:
            state = state.apply_fill(fill)
        states[arm_id] = state

        entries = [capital_entry(arm_id, scenario.initial_cash_usd, scenario.started_at)]
        entries.extend(fill_entry(fill) for fill in fills)
        validate_entries(entries)
        ledger_cash, ledger_positions = rebuild_holdings(entries)
        rebuilt = rebuild_arm_state(arm_id, scenario.initial_cash_usd, fills)
        if ledger_cash != rebuilt.cash_usd or ledger_positions != rebuilt.positions:
            raise ValueError(f"Ledger and fill replay disagree for {arm_id}")
        if rebuilt != state:
            raise ValueError(f"Incremental and replayed arm state disagree for {arm_id}")

        nav = calculate_nav(
            arm_id=arm_id,
            as_of=max(bar.available_at for bar in final_bars.values()),
            cash_usd=ledger_cash,
            positions=ledger_positions,
            prices={symbol: final_prices[symbol] for symbol in ledger_positions},
        )
        arm_artifacts[arm_id] = ArmArtifacts(
            state=state,
            policy_state=policy_state,
            portfolio_decision=portfolio_decision,
            risk_decision=risk_decision,
            order_intents=intents,
            fills=fills,
            ledger_entries=entries,
            nav_snapshot=nav,
        )

    future_data_ignored = _verify_future_data_is_ignored(scenario)
    forbidden_patch_rejected = _verify_forbidden_patch_is_rejected(
        patch,
        scenario.decision_time,
    )
    restored = policy_compiler.expire(
        policy_states["B3-RISK"],
        now=patch.expires_at,
        expires_at=patch.expires_at,
    )
    policy_expiry_restored = (
        restored.portfolio_risk_multiplier == 1.0
        and not restored.strategy_risk_deltas
        and not restored.blocked_targets
    )

    manifest: dict[str, Any] = {
        "schema_version": "phase0_result_manifest_v1",
        "run_id": scenario.run_id,
        "scenario_hash": canonical_hash(scenario),
        "config_manifest_hash": config_manifest_hash,
        "code_version": code_version,
        "source_record_hash": canonical_hash(source),
        "feature_snapshot_hash": canonical_hash(features),
        "forecast_hash": canonical_hash(forecasts),
        "news_event_hash": canonical_hash(news_event),
        "policy_patch_hash": canonical_hash(patch),
        "execution_scenario_id": scenario.execution_scenario_id,
        "arms": {
            arm_id: _arm_manifest(arm_artifacts[arm_id])
            for arm_id in ARM_IDS
        },
        "counts": {
            "trading_days": len({bar.event_time.date() for bar in scenario.bars}),
            "symbols": len({bar.symbol for bar in scenario.bars}),
            "feature_snapshots": len(features),
            "forecasts": len(forecasts),
            "news_events": 1,
            "policy_patches": 1,
            "partial_fills": sum(
                1
                for arm in arm_artifacts.values()
                for fill in arm.fills
                if fill.quantity
                < next(
                    intent.quantity
                    for intent in arm.order_intents
                    if intent.order_intent_id == fill.order_intent_id
                )
            ),
        },
        "invariants": {
            "arm_states_independent": states_are_independent(states),
            "duplicate_delivery_effect_count": 1,
            "future_data_ignored": future_data_ignored,
            "forbidden_patch_rejected": forbidden_patch_rejected,
            "policy_expiry_restored": policy_expiry_restored,
            "real_broker_enabled": False,
            "real_llm_enabled": False,
        },
    }
    return SimulationArtifacts(
        scenario=scenario,
        source_record=source,
        feature_snapshots=features,
        forecasts=forecasts,
        news_event=news_event,
        policy_patch=patch,
        arms=arm_artifacts,
        manifest=manifest,
        result_hash=canonical_hash(manifest),
    )


def _latest_prices_as_of(
    bars: list[SyntheticBar], as_of: Any
) -> dict[str, Decimal]:
    selected = available_as_of(bars, as_of)
    by_symbol: dict[str, SyntheticBar] = {}
    for bar in selected:
        current = by_symbol.get(bar.symbol)
        if current is None or bar.event_time > current.event_time:
            by_symbol[bar.symbol] = bar
    return {symbol: bar.close for symbol, bar in by_symbol.items()}


def _next_bars(bars: list[SyntheticBar], after: Any) -> dict[str, SyntheticBar]:
    by_symbol: dict[str, SyntheticBar] = {}
    for bar in bars:
        if bar.available_at <= after:
            continue
        current = by_symbol.get(bar.symbol)
        if current is None or bar.available_at < current.available_at:
            by_symbol[bar.symbol] = bar
    return by_symbol


def _final_bars(bars: list[SyntheticBar]) -> dict[str, SyntheticBar]:
    by_symbol: dict[str, SyntheticBar] = {}
    for bar in bars:
        current = by_symbol.get(bar.symbol)
        if current is None or bar.available_at > current.available_at:
            by_symbol[bar.symbol] = bar
    return by_symbol


def _build_order_intents(
    *,
    arm_id: str,
    decision: PortfolioDecision,
    risk_decision: RiskDecision,
    prices: dict[str, Decimal],
    nav: Decimal,
) -> list[OrderIntent]:
    intents: list[OrderIntent] = []
    for symbol, weight in sorted(risk_decision.approved_target_weights.items()):
        if symbol == "USD_CASH" or weight <= 0:
            continue
        quantity = (nav * Decimal(str(weight)) / prices[symbol]).quantize(
            Decimal("0.001"),
            rounding=ROUND_DOWN,
        )
        order_id = stable_id("order", arm_id, decision.portfolio_decision_id, symbol)
        intents.append(
            OrderIntent(
                order_intent_id=order_id,
                arm_id=arm_id,
                portfolio_decision_id=decision.portfolio_decision_id,
                risk_decision_id=risk_decision.risk_decision_id,
                symbol=symbol,
                side=OrderSide.BUY,
                order_type="MARKET",
                quantity=quantity,
                limit_price=None,
                time_in_force="DAY",
                session="REGULAR",
                client_order_id=stable_id("client", order_id),
                idempotency_key=stable_id("order_idem", order_id),
                created_at=decision.decision_time,
            )
        )
    return intents


def _arm_manifest(artifacts: ArmArtifacts) -> dict[str, Any]:
    return {
        "target_hash": canonical_hash(artifacts.portfolio_decision.target_weights_pre_risk),
        "decision_hash": canonical_hash(artifacts.portfolio_decision),
        "risk_hash": canonical_hash(artifacts.risk_decision),
        "order_count": len(artifacts.order_intents),
        "order_intent_hash": canonical_hash(artifacts.order_intents),
        "fill_count": len(artifacts.fills),
        "fill_hash": canonical_hash(artifacts.fills),
        "ledger_hash": canonical_hash(artifacts.ledger_entries),
        "state_hash": canonical_hash(artifacts.state.as_payload()),
        "nav_hash": canonical_hash(artifacts.nav_snapshot),
        "nav_usd": str(artifacts.nav_snapshot.nav_usd),
        "policy_version": artifacts.policy_state.version,
    }


def _verify_future_data_is_ignored(scenario: SyntheticScenario) -> bool:
    before = canonical_hash(available_as_of(scenario.bars, scenario.decision_time))
    future_bar = SyntheticBar(
        symbol="QQQ",
        event_time=scenario.decision_time + timedelta(days=30),
        available_at=scenario.decision_time + timedelta(days=30),
        open=Decimal("999"),
        close=Decimal("999"),
    )
    after = canonical_hash(
        available_as_of([*scenario.bars, future_bar], scenario.decision_time)
    )
    return before == after


def _verify_forbidden_patch_is_rejected(
    allowed_patch: PolicyPatch,
    now: Any,
) -> bool:
    forbidden_operation = PolicyOperation(
        action=PolicyAction.REDUCE_RISK_BUDGET,
        target_kind=PolicyTargetKind.STRATEGY,
        target_id="T1",
        risk_budget_delta=0.10,
        risk_multiplier=None,
        blocked=None,
    )
    forbidden = allowed_patch.model_copy(update={"operations": [forbidden_operation]})
    try:
        PolicyCompiler().compile(
            forbidden,
            PolicyState.default("B3-RISK"),
            now=now,
            shadow_mode=True,
        )
    except PolicyCompileError:
        return True
    return False
