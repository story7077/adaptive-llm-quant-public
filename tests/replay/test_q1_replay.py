from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TypedDict

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from trading.domain.contracts import Fill, model_payload
from trading.domain.enums import OrderSide
from trading.domain.hashing import canonical_hash, stable_id
from trading.domain.q1 import (
    MarketCalendarSession,
    MatchedAttributionResult,
    MatchedComparison,
    OrderEventType,
    PointInTimeSourceReference,
    Q1ArmId,
    Q1DecisionInputManifest,
    Q1StrategyDecision,
    RiskEpisode,
    RiskEpisodeEvent,
    RiskEpisodeEventType,
    RiskSeverity,
    RiskTarget,
    StrategyDailyResult,
    StrategyEvaluationAnchor,
)
from trading.domain.q1_runtime import Q1Fill, Q1OrderIntent
from trading.execution.order_state import (
    OrderDescriptor,
    OrderEventProvenance,
    Q1OrderClass,
    append_order_event,
)
from trading.persistence.db import (
    create_database_engine,
    make_session_factory,
    upgrade_database,
)
from trading.persistence.models import (
    CashSettlementEventRow,
    FillRow,
    MatchedAttributionResultRow,
    OrderEventRow,
    PaperCycleRow,
    RiskEpisodeTargetRow,
    RunRow,
    StrategyDailyResultRow,
)
from trading.persistence.q1 import (
    CashSettlementRepository,
    MarketCalendarSessionRepository,
    OrderEventRepository,
    Q1StrategyDecisionRepository,
    RiskEpisodeRepository,
    StrategyEvaluationAnchorRepository,
    StrategyEvaluationResultRepository,
)
from trading.persistence.q1_runtime import (
    append_arm_state,
    append_fill,
    append_nav_snapshot,
    append_order_intent,
)
from trading.replay.q1 import replay_q1_run
from trading.runtime.q1_planning import (
    build_portfolio_decision,
    risk_approval_id,
)
from trading.runtime.q1_state import Q1ArmState
from trading.settlement.service import (
    SettlementPolicy,
    SettlementProvenance,
    record_buy_cash_debit,
    record_opening_settled_cash,
)

RUN_ID = "q1-replay-run"
ALGORITHM_VERSION = "q1_math_core_v1"
CONFIG_HASH = "a" * 64
SOURCE_HASH = "b" * 64
CODE_VERSION = "q1-replay-test"
MODEL_VERSION = "deterministic-none"
LEASE_OWNER = "q1-replay-worker"
SESSION_DATE = date(2026, 7, 27)
OPEN_AT = datetime(2026, 7, 27, 13, 30, tzinfo=UTC)
CLOSE_AT = OPEN_AT + timedelta(hours=6, minutes=30)
DECISION_AT = OPEN_AT + timedelta(minutes=30)
FILL_AT = DECISION_AT + timedelta(minutes=1)
CYCLE_ID = "q1-replay-cycle"
CALENDAR_SESSION_ID = "q1-replay-calendar-session"
INITIAL_NAV = Decimal("10000")
FILL_QUANTITY = Decimal("2")
FILL_PRICE = Decimal("500.01")
FILL_COMMISSION = Decimal("0.02")
TRADED_ARMS = (
    Q1ArmId.B0_QQQ,
    Q1ArmId.B0_VOL,
    Q1ArmId.Q1_DET,
    Q1ArmId.Q1_LLM,
)
STRATEGY_ARMS = (
    Q1ArmId.B0_CASH,
    Q1ArmId.B0_QQQ,
    Q1ArmId.B0_VOL,
    Q1ArmId.Q1_DET,
    Q1ArmId.Q1_LLM,
)


class _VersionFields(TypedDict):
    algorithm_version: str
    config_manifest_hash: str
    code_version: str
    model_version: str
    source_manifest_hash: str


def _version_fields() -> _VersionFields:
    return {
        "algorithm_version": ALGORITHM_VERSION,
        "config_manifest_hash": CONFIG_HASH,
        "code_version": CODE_VERSION,
        "model_version": MODEL_VERSION,
        "source_manifest_hash": SOURCE_HASH,
    }


def _settlement_policy() -> SettlementPolicy:
    return SettlementPolicy(
        version="settlement-tplus1-test-v1",
        calendar_version="XNYS-test-v1",
        lag_business_sessions=1,
    )


def _settlement_provenance() -> SettlementProvenance:
    return SettlementProvenance(
        run_id=RUN_ID,
        source_cycle_id=CYCLE_ID,
        config_manifest_hash=CONFIG_HASH,
        code_version=CODE_VERSION,
        model_version=MODEL_VERSION,
        source_manifest_hash=SOURCE_HASH,
        worker_fence_token=LEASE_OWNER,
        cycle_attempt_count=1,
    )


def _anchor() -> StrategyEvaluationAnchor:
    content = {
        "run_id": RUN_ID,
        "calendar_session_id": CALENDAR_SESSION_ID,
        "common_t0_at": OPEN_AT,
        "initial_nav_usd": INITIAL_NAV,
        "quote_manifest_hash": SOURCE_HASH,
        "config_manifest_hash": CONFIG_HASH,
        "code_version": CODE_VERSION,
        "model_version": MODEL_VERSION,
        "source_manifest_hash": SOURCE_HASH,
    }
    anchor_hash = canonical_hash(content)
    return StrategyEvaluationAnchor(
        evaluation_anchor_id=stable_id(
            "q1-evaluation-anchor",
            RUN_ID,
            anchor_hash,
        ),
        run_id=RUN_ID,
        calendar_session_id=CALENDAR_SESSION_ID,
        common_t0_at=OPEN_AT,
        initial_nav_usd=INITIAL_NAV,
        quote_manifest_hash=SOURCE_HASH,
        anchor_hash=anchor_hash,
        created_at=OPEN_AT,
        **_version_fields(),
    )


def _nav_payload(
    *,
    state: Q1ArmState,
    prices: dict[str, Decimal],
    baseline: bool,
) -> tuple[Decimal, dict[str, object]]:
    nav = state.nav(prices)
    positions_market_value = nav - state.total_cash_usd
    payload: dict[str, object] = {
        "schema_version": "q1_nav_v1",
        "calendar_session_id": CALENDAR_SESSION_ID,
        "settled_cash_usd": str(state.settled_cash_usd),
        "unsettled_receivables_usd": str(state.unsettled_cash_usd),
        "positions_market_value_usd": str(positions_market_value),
        "actual_weights": {
            symbol: str(weight)
            for symbol, weight in state.weights(prices).items()
        },
        "risk_state": "NOT_APPLICABLE",
        "release_condition_valid": False,
        "reconciliation_ok": True,
        "reconciliation_status": "OK",
        "real_order_routing": False,
    }
    if baseline:
        payload["session_open_baseline"] = True
    return nav, payload


def _append_nav(
    session: Session,
    *,
    state: Q1ArmState,
    as_of: datetime,
    prices: dict[str, Decimal],
    baseline: bool,
    tamper_economics: bool = False,
) -> None:
    nav, payload = _nav_payload(
        state=state,
        prices=prices,
        baseline=baseline,
    )
    if tamper_economics:
        payload["positions_market_value_usd"] = "999.99"
    append_nav_snapshot(
        session,
        run_id=RUN_ID,
        arm_id=state.arm_id,
        source_cycle_id=CYCLE_ID,
        as_of=as_of,
        nav_usd=nav,
        payload=payload,
        quote_manifest_hash=SOURCE_HASH,
        algorithm_version=ALGORITHM_VERSION,
        config_manifest_hash=CONFIG_HASH,
        code_version=CODE_VERSION,
        model_version=MODEL_VERSION,
        source_manifest_hash=SOURCE_HASH,
    )


def _decision_input_manifest(arm_id: Q1ArmId) -> Q1DecisionInputManifest:
    quote = PointInTimeSourceReference(
        record_id=f"decision-quote-{arm_id.value}",
        available_at=DECISION_AT,
    )
    manifest_hash = canonical_hash(
        {
            **_version_fields(),
            "calendar_session_id": CALENDAR_SESSION_ID,
            "source_bars": (),
            "quotes": (quote,),
        }
    )
    return Q1DecisionInputManifest(
        algorithm_version=ALGORITHM_VERSION,
        config_manifest_hash=CONFIG_HASH,
        code_version=CODE_VERSION,
        model_version=MODEL_VERSION,
        source_manifest_hash=SOURCE_HASH,
        calendar_session_id=CALENDAR_SESSION_ID,
        source_bars=(),
        quotes=(quote,),
        manifest_hash=manifest_hash,
    )


def _target_weights(arm_id: Q1ArmId) -> dict[str, Decimal]:
    if arm_id is Q1ArmId.B0_CASH:
        return {
            "QQQ": Decimal("0"),
            "SOXX": Decimal("0"),
            "USD_CASH": Decimal("1"),
        }
    if arm_id is Q1ArmId.B0_QQQ:
        return {
            "QQQ": Decimal("1"),
            "SOXX": Decimal("0"),
            "USD_CASH": Decimal("0"),
        }
    return {
        "QQQ": Decimal("0.10"),
        "SOXX": Decimal("0"),
        "USD_CASH": Decimal("0.90"),
    }


def _build_decision(arm_id: Q1ArmId) -> Q1StrategyDecision:
    targets = _target_weights(arm_id)
    return build_portfolio_decision(
        run_id=RUN_ID,
        arm_id=arm_id,
        source_cycle_id=CYCLE_ID,
        input_state_sequence=0,
        decision_kind=(
            "NO_TRADE"
            if arm_id is Q1ArmId.B0_CASH
            else "NORMAL_TARGET"
        ),
        scheduled_at=DECISION_AT,
        signal_data_cutoff=DECISION_AT,
        portfolio_state_as_of=DECISION_AT,
        quote_as_of=DECISION_AT,
        decision_created_at=DECISION_AT,
        valid_until=DECISION_AT + timedelta(minutes=20),
        current_weights={"USD_CASH": Decimal("1")},
        deterministic_target_weights=targets,
        final_target_weights=targets,
        expected_annualized_volatility=Decimal("0.015"),
        expected_one_way_turnover=(
            Decimal("0")
            if arm_id is Q1ArmId.B0_CASH
            else Decimal("0.10")
        ),
        used_daily_turnover_before=Decimal("0"),
        signal_hash=SOURCE_HASH,
        allocation_hash=SOURCE_HASH,
        llm_overlay_state="NO_CHANGE",
        llm_policy_id=None,
        diagnostics={"fixture": "complete-synthetic-session"},
        input_manifest=_decision_input_manifest(arm_id),
        worker_fence_token=LEASE_OWNER,
        cycle_attempt_count=1,
    )


def _order_intent(
    *,
    decision: Q1StrategyDecision,
    tamper_hash: bool,
) -> Q1OrderIntent:
    quote_id = f"decision-quote-{decision.arm_id.value}"
    risk_decision_id = risk_approval_id(decision)
    identity = {
        "run_id": RUN_ID,
        "arm_id": decision.arm_id,
        "portfolio_decision_id": decision.portfolio_decision_id,
        "source_cycle_id": CYCLE_ID,
        "symbol": "QQQ",
        "side": OrderSide.BUY,
        "quantity": FILL_QUANTITY,
        "decision_quote_id": quote_id,
        "created_at": DECISION_AT,
        "valid_until": decision.valid_until,
    }
    intent_hash = canonical_hash(
        {
            **identity,
            "risk_decision_id": risk_decision_id,
            "decision_reference_price": Decimal("500"),
            "decision_spread_bps": Decimal("2"),
            "input_state_sequence": 0,
            "config_manifest_hash": CONFIG_HASH,
            "code_version": CODE_VERSION,
            "model_version": MODEL_VERSION,
            "source_manifest_hash": SOURCE_HASH,
        }
    )
    if tamper_hash:
        intent_hash = "f" * 64
    return Q1OrderIntent(
        order_intent_id=stable_id("q1-order-intent", intent_hash),
        run_id=RUN_ID,
        arm_id=decision.arm_id,
        portfolio_decision_id=decision.portfolio_decision_id,
        risk_decision_id=risk_decision_id,
        source_cycle_id=CYCLE_ID,
        input_state_sequence=0,
        symbol="QQQ",
        side=OrderSide.BUY,
        order_class=Q1OrderClass.NORMAL.value,
        quantity=FILL_QUANTITY,
        decision_quote_id=quote_id,
        decision_reference_price=Decimal("500"),
        decision_spread_bps=Decimal("2"),
        created_at=DECISION_AT,
        valid_until=decision.valid_until,
        idempotency_key=stable_id(
            "q1-order-intent-idem",
            intent_hash,
        ),
        intent_hash=intent_hash,
        **_version_fields(),
    )


def _fill(intent: Q1OrderIntent, *, tamper_hash: bool) -> Q1Fill:
    quote_id = f"fill-quote-{intent.arm_id.value}"
    fill_id = stable_id(
        "q1-fill",
        intent.order_intent_id,
        quote_id,
        "BASE",
    )
    template = Q1Fill(
        fill_id=fill_id,
        order_intent_id=intent.order_intent_id,
        run_id=RUN_ID,
        arm_id=intent.arm_id,
        source_cycle_id=CYCLE_ID,
        quote_id=quote_id,
        quote_event_time=FILL_AT,
        quote_available_at=FILL_AT,
        symbol="QQQ",
        side=OrderSide.BUY,
        quantity=FILL_QUANTITY,
        price=FILL_PRICE,
        commission_usd=FILL_COMMISSION,
        cumulative_order_commission_usd=FILL_COMMISSION,
        execution_scenario_id="BASE",
        base_fill_cost_usd=Decimal("0.04"),
        sensitivity_5bp_cost_usd=Decimal("0.54"),
        sensitivity_10bp_cost_usd=Decimal("1.04"),
        effective_at=FILL_AT,
        created_at=FILL_AT,
        fill_hash="placeholder",
        **_version_fields(),
    )
    valid_hash = canonical_hash(template.model_dump(exclude={"fill_hash"}))
    return template.model_copy(
        update={"fill_hash": "e" * 64 if tamper_hash else valid_hash}
    )


def _order_descriptor(intent: Q1OrderIntent) -> OrderDescriptor:
    return OrderDescriptor(
        order_intent_id=intent.order_intent_id,
        arm_id=intent.arm_id.value,
        portfolio_decision_id=intent.portfolio_decision_id,
        symbol=intent.symbol,
        side=intent.side,
        quantity=intent.quantity,
        order_class=Q1OrderClass.NORMAL,
        created_at=intent.created_at,
        valid_until=intent.valid_until,
    )


def _order_provenance() -> OrderEventProvenance:
    return OrderEventProvenance(
        config_manifest_hash=CONFIG_HASH,
        code_version=CODE_VERSION,
        model_version=MODEL_VERSION,
        source_manifest_hash=SOURCE_HASH,
        worker_fence_token=LEASE_OWNER,
        cycle_attempt_count=1,
    )


def _append_trade(
    session: Session,
    *,
    decision: Q1StrategyDecision,
    initial_state: Q1ArmState,
    tamper_fill_hash: bool,
    tamper_intent_hash: bool,
    tamper_nav_economics: bool,
) -> Q1ArmState:
    intent = _order_intent(
        decision=decision,
        tamper_hash=tamper_intent_hash,
    )
    append_order_intent(session, intent)
    session.flush()
    order = _order_descriptor(intent)
    created = append_order_event(
        order=order,
        existing_events=(),
        event_type=OrderEventType.CREATED,
        occurred_at=DECISION_AT,
        available_at=DECISION_AT,
        provenance=_order_provenance(),
        source_cycle_id=CYCLE_ID,
    )
    OrderEventRepository(session).append(created)
    fill = _fill(intent, tamper_hash=tamper_fill_hash)
    append_fill(session, fill)
    CashSettlementRepository(session).append(
        record_buy_cash_debit(
            arm_id=intent.arm_id,
            fill_id=fill.fill_id,
            trade_at=fill.effective_at,
            fill_notional_usd=fill.quantity * fill.price,
            commission_usd=fill.commission_usd,
            created_at=fill.created_at,
            calendar_session_id=CALENDAR_SESSION_ID,
            policy=_settlement_policy(),
            provenance=_settlement_provenance(),
        )
    )
    filled = append_order_event(
        order=order,
        existing_events=(created,),
        event_type=OrderEventType.FILLED,
        occurred_at=fill.effective_at,
        available_at=fill.created_at,
        provenance=_order_provenance(),
        quantity_delta=fill.quantity,
        commission_delta_usd=fill.commission_usd,
        source_id=fill.fill_id,
        quote_id=fill.quote_id,
        source_cycle_id=CYCLE_ID,
    )
    OrderEventRepository(session).append(filled)
    next_state = initial_state.apply_fill(
        Fill(
            fill_id=fill.fill_id,
            order_intent_id=fill.order_intent_id,
            arm_id=fill.arm_id.value,
            symbol=fill.symbol,
            side=fill.side,
            quantity=fill.quantity,
            price=fill.price,
            commission_usd=fill.commission_usd,
            execution_scenario_id=fill.execution_scenario_id,
            effective_at=fill.effective_at,
            created_at=fill.created_at,
        )
    )
    append_arm_state(
        session,
        run_id=RUN_ID,
        state=next_state,
        source_cycle_id=CYCLE_ID,
        created_at=fill.created_at,
        expected_previous_sequence=0,
    )
    _append_nav(
        session,
        state=next_state,
        as_of=fill.created_at,
        prices={"QQQ": fill.price},
        baseline=False,
        tamper_economics=tamper_nav_economics,
    )
    return next_state


def _risk_episode() -> tuple[RiskEpisode, RiskEpisodeEvent]:
    triggered_at = FILL_AT + timedelta(seconds=1)
    target = RiskTarget(
        symbol="QQQ",
        target_quantity=Decimal("1"),
        trigger_quote_id=f"fill-quote-{Q1ArmId.Q1_DET.value}",
        target_generation=1,
        trigger_quantity=FILL_QUANTITY,
        trigger_price=FILL_PRICE,
        target_weight=Decimal("0.05"),
    )
    target_manifest_hash = canonical_hash((target,))
    identity = {
        "run_id": RUN_ID,
        "arm_id": Q1ArmId.Q1_DET,
        "calendar_session_id": CALENDAR_SESSION_ID,
        "severity": RiskSeverity.HARD_REDUCE,
        "triggered_at": triggered_at,
        "target_manifest_hash": target_manifest_hash,
    }
    episode_id = stable_id("q1-risk-episode", identity)
    episode_hash = canonical_hash(
        {
            **identity,
            "trigger_nav_usd": Decimal("9999.98"),
            "daily_loss": Decimal("0.000002"),
            "run_drawdown": Decimal("0.1666683333333333333333333333"),
            "config_manifest_hash": CONFIG_HASH,
            "code_version": CODE_VERSION,
            "model_version": MODEL_VERSION,
            "source_manifest_hash": SOURCE_HASH,
        }
    )
    episode = RiskEpisode(
        risk_episode_id=episode_id,
        run_id=RUN_ID,
        arm_id=Q1ArmId.Q1_DET,
        severity=RiskSeverity.HARD_REDUCE,
        calendar_session_id=CALENDAR_SESSION_ID,
        triggered_at=triggered_at,
        trigger_nav_usd=Decimal("9999.98"),
        session_open_nav_usd=INITIAL_NAV,
        running_peak_nav_usd=Decimal("12000"),
        daily_loss=Decimal("0.000002"),
        run_drawdown=Decimal("0.1666683333333333333333333333"),
        portfolio_annualized_vol=Decimal("0.15"),
        soft_daily_threshold=Decimal("0.015"),
        hard_daily_threshold=Decimal("0.025"),
        reconciliation_status="OK",
        targets=(target,),
        target_manifest_hash=target_manifest_hash,
        episode_hash=episode_hash,
        created_at=triggered_at,
        **_version_fields(),
    )
    event_identity = {
        "risk_episode_id": episode_id,
        "event_type": RiskEpisodeEventType.ACTIVATE,
        "event_sequence": 1,
        "severity": RiskSeverity.HARD_REDUCE,
        "target_generation": 1,
        "targets": (target,),
        "target_symbol": None,
        "observed_quantity": None,
        "residual_quantity": None,
        "occurred_at": triggered_at,
        "source_cycle_id": CYCLE_ID,
    }
    event_id = stable_id("q1-risk-episode-event", event_identity)
    event = RiskEpisodeEvent(
        risk_episode_event_id=event_id,
        risk_episode_id=episode_id,
        event_type=RiskEpisodeEventType.ACTIVATE,
        event_sequence=1,
        severity=RiskSeverity.HARD_REDUCE,
        target_generation=1,
        occurred_at=triggered_at,
        available_at=triggered_at,
        targets=(target,),
        source_cycle_id=CYCLE_ID,
        worker_fence_token=LEASE_OWNER,
        cycle_attempt_count=1,
        idempotency_key=stable_id("q1-risk-event-idem", event_id),
        event_hash=canonical_hash(
            {
                **event_identity,
                "config_manifest_hash": CONFIG_HASH,
                "code_version": CODE_VERSION,
                "model_version": MODEL_VERSION,
                "source_manifest_hash": SOURCE_HASH,
            }
        ),
        **_version_fields(),
    )
    return episode, event


def _daily_result(
    *,
    arm_id: Q1ArmId,
    state: Q1ArmState,
    prices: dict[str, Decimal],
    tamper_hash: bool,
) -> StrategyDailyResult:
    nav = state.nav(prices)
    net_return = nav / INITIAL_NAV - Decimal("1")
    weights = state.weights(prices)
    template = StrategyDailyResult(
        strategy_daily_result_id="placeholder",
        evaluation_anchor_id=state.evaluation_anchor_id or "",
        run_id=RUN_ID,
        arm_id=arm_id,
        calendar_session_id=CALENDAR_SESSION_ID,
        session_date=SESSION_DATE,
        valuation_at=CLOSE_AT,
        nav_usd=nav,
        net_daily_return=net_return,
        cumulative_return=net_return,
        daily_turnover=(
            Decimal("0.10") if arm_id in TRADED_ARMS else Decimal("0")
        ),
        cumulative_turnover=(
            Decimal("0.10") if arm_id in TRADED_ARMS else Decimal("0")
        ),
        commissions_usd=(
            FILL_COMMISSION if arm_id in TRADED_ARMS else Decimal("0")
        ),
        spread_cost_usd=(
            Decimal("0.02") if arm_id in TRADED_ARMS else Decimal("0")
        ),
        delay_cost_usd=(
            Decimal("0.02") if arm_id in TRADED_ARMS else Decimal("0")
        ),
        sensitivity_5bp_usd=(
            Decimal("0.54") if arm_id in TRADED_ARMS else Decimal("0")
        ),
        sensitivity_10bp_usd=(
            Decimal("1.04") if arm_id in TRADED_ARMS else Decimal("0")
        ),
        cash_weight=weights["USD_CASH"],
        qqq_weight=weights.get("QQQ", Decimal("0")),
        soxx_weight=Decimal("0"),
        active_risk_episode_count=(
            1 if arm_id is Q1ArmId.Q1_DET else 0
        ),
        active_llm_reduction_count=0,
        result_hash="placeholder",
        created_at=CLOSE_AT,
        **_version_fields(),
    )
    result_hash = canonical_hash(
        template.model_dump(
            exclude={"strategy_daily_result_id", "result_hash"}
        )
    )
    if tamper_hash:
        result_hash = "d" * 64
    return template.model_copy(
        update={
            "strategy_daily_result_id": stable_id(
                "q1-daily-result",
                RUN_ID,
                arm_id,
                SESSION_DATE,
                result_hash,
            ),
            "result_hash": result_hash,
        }
    )


def _matched_result(
    *,
    comparison: MatchedComparison,
    left: StrategyDailyResult,
    right: StrategyDailyResult,
) -> MatchedAttributionResult:
    mean = left.net_daily_return - right.net_daily_return
    template = MatchedAttributionResult(
        matched_attribution_result_id="placeholder",
        evaluation_anchor_id=left.evaluation_anchor_id,
        run_id=RUN_ID,
        comparison=comparison,
        left_arm_id=left.arm_id,
        right_arm_id=right.arm_id,
        through_session_date=SESSION_DATE,
        common_valid_sessions=1,
        mean_daily_difference=mean,
        annualized_difference=mean * Decimal("252"),
        newey_west_lag=5,
        newey_west_standard_error=Decimal("0"),
        bootstrap_seed=7077,
        bootstrap_lower=mean,
        bootstrap_upper=mean,
        promotion_ready=False,
        result_hash="placeholder",
        created_at=CLOSE_AT,
        **_version_fields(),
    )
    result_hash = canonical_hash(
        template.model_dump(
            exclude={"matched_attribution_result_id", "result_hash"}
        )
    )
    return template.model_copy(
        update={
            "matched_attribution_result_id": stable_id(
                "q1-matched-result",
                RUN_ID,
                comparison,
                SESSION_DATE,
                result_hash,
            ),
            "result_hash": result_hash,
        }
    )


def _append_orphan_risk_target(
    session: Session,
    *,
    episode: RiskEpisode,
) -> None:
    target = RiskTarget(
        symbol="SOXX",
        target_quantity=Decimal("0"),
        trigger_quote_id="orphan-trigger-quote",
        target_generation=2,
        trigger_quantity=Decimal("1"),
        trigger_price=Decimal("250"),
        target_weight=Decimal("0"),
    )
    target_hash = canonical_hash(target)
    session.add(
        RiskEpisodeTargetRow(
            risk_target_id=stable_id(
                "q1-risk-target",
                episode.risk_episode_id,
                target.target_generation,
                target.symbol,
                target_hash,
            ),
            risk_episode_id=episode.risk_episode_id,
            symbol=target.symbol,
            target_generation=target.target_generation,
            target_quantity=target.target_quantity,
            trigger_quantity=target.trigger_quantity,
            trigger_price=target.trigger_price,
            trigger_quote_id=target.trigger_quote_id,
            target_weight=target.target_weight,
            config_manifest_hash=CONFIG_HASH,
            target_hash=target_hash,
            payload_json=model_payload(target),
            created_at=FILL_AT + timedelta(seconds=2),
        )
    )


def _seed_q1_replay_session(
    session: Session,
    *,
    tamper_fill_hash: bool = False,
    tamper_intent_hash: bool = False,
    tamper_nav_economics: bool = False,
    tamper_daily_hash: bool = False,
    orphan_risk_target: bool = False,
) -> None:
    session.add(
        RunRow(
            run_id=RUN_ID,
            mode="PAPER",
            experiment_version=ALGORITHM_VERSION,
            config_manifest_hash=CONFIG_HASH,
            code_commit=CODE_VERSION,
            started_at=OPEN_AT,
            ended_at=None,
            status="RUNNING",
            result_manifest={"real_order_routing": False},
            result_hash=None,
        )
    )
    session.flush()
    session.add(
        PaperCycleRow(
            cycle_id=CYCLE_ID,
            run_id=RUN_ID,
            cycle_kind="Q1_STRATEGIC",
            scheduled_at=DECISION_AT,
            data_available_cutoff=DECISION_AT,
            status="RUNNING",
            idempotency_key=CYCLE_ID,
            lease_owner=LEASE_OWNER,
            lease_expires_at=CLOSE_AT + timedelta(hours=1),
            attempt_count=1,
            input_manifest_hash=SOURCE_HASH,
            output_manifest_hash=None,
            started_at=DECISION_AT,
            completed_at=None,
            last_error_code=None,
            last_error_detail=None,
            created_at=DECISION_AT,
            updated_at=DECISION_AT,
        )
    )
    session.flush()
    calendar = MarketCalendarSession(
        calendar_session_id=CALENDAR_SESSION_ID,
        calendar_version="XNYS-test-v1",
        session_date=SESSION_DATE,
        open_at=OPEN_AT,
        close_at=CLOSE_AT,
        source="test-calendar",
        available_at=OPEN_AT - timedelta(days=1),
        session_hash=canonical_hash({"session": SESSION_DATE}),
        created_at=OPEN_AT - timedelta(days=1),
        **_version_fields(),
    )
    MarketCalendarSessionRepository(session).append(calendar)
    anchor = _anchor()
    StrategyEvaluationAnchorRepository(session).append(anchor)

    cash_repository = CashSettlementRepository(session)
    states: dict[Q1ArmId, Q1ArmState] = {}
    for arm_id in Q1ArmId:
        state = Q1ArmState(
            arm_id=arm_id.value,
            initial_nav_usd=INITIAL_NAV,
            settled_cash_usd=INITIAL_NAV,
            unsettled_receivables=(),
            positions={},
            sequence=0,
            evaluation_anchor_id=anchor.evaluation_anchor_id,
        )
        states[arm_id] = state
        append_arm_state(
            session,
            run_id=RUN_ID,
            state=state,
            source_cycle_id=CYCLE_ID,
            created_at=OPEN_AT,
            expected_previous_sequence=None,
        )
        cash_repository.append(
            record_opening_settled_cash(
                arm_id=arm_id,
                amount_usd=INITIAL_NAV,
                effective_at=OPEN_AT,
                created_at=OPEN_AT,
                calendar_session_id=CALENDAR_SESSION_ID,
                policy=_settlement_policy(),
                provenance=_settlement_provenance(),
            )
        )
        _append_nav(
            session,
            state=state,
            as_of=OPEN_AT,
            prices={},
            baseline=True,
        )

    decisions = {
        arm_id: _build_decision(arm_id)
        for arm_id in STRATEGY_ARMS
    }
    decision_repository = Q1StrategyDecisionRepository(session)
    for decision in decisions.values():
        decision_repository.append(decision)
    for arm_id in TRADED_ARMS:
        states[arm_id] = _append_trade(
            session,
            decision=decisions[arm_id],
            initial_state=states[arm_id],
            tamper_fill_hash=(
                tamper_fill_hash and arm_id is Q1ArmId.Q1_DET
            ),
            tamper_intent_hash=(
                tamper_intent_hash and arm_id is Q1ArmId.Q1_DET
            ),
            tamper_nav_economics=(
                tamper_nav_economics and arm_id is Q1ArmId.Q1_DET
            ),
        )

    episode, activation = _risk_episode()
    RiskEpisodeRepository(session).append_episode(episode, activation)
    if orphan_risk_target:
        _append_orphan_risk_target(session, episode=episode)

    daily_repository = StrategyEvaluationResultRepository(session)
    daily_results: dict[Q1ArmId, StrategyDailyResult] = {}
    for arm_id in Q1ArmId:
        prices = {"QQQ": FILL_PRICE} if arm_id in TRADED_ARMS else {}
        result = _daily_result(
            arm_id=arm_id,
            state=states[arm_id],
            prices=prices,
            tamper_hash=(
                tamper_daily_hash and arm_id is Q1ArmId.Q1_DET
            ),
        )
        daily_results[arm_id] = result
        daily_repository.append_daily(result)
    daily_repository.append_matched(
        _matched_result(
            comparison=MatchedComparison.Q1_DET_MINUS_B0_VOL,
            left=daily_results[Q1ArmId.Q1_DET],
            right=daily_results[Q1ArmId.B0_VOL],
        )
    )
    daily_repository.append_matched(
        _matched_result(
            comparison=MatchedComparison.Q1_LLM_MINUS_Q1_DET,
            left=daily_results[Q1ArmId.Q1_LLM],
            right=daily_results[Q1ArmId.Q1_DET],
        )
    )


def _seed_q1_replay(
    factory: sessionmaker[Session],
    **kwargs: bool,
) -> None:
    with factory.begin() as session:
        _seed_q1_replay_session(session, **kwargs)


def _new_database(
    path: Path,
) -> tuple[Engine, sessionmaker[Session]]:
    database_url = f"sqlite+pysqlite:///{path.as_posix()}"
    upgrade_database(database_url)
    engine = create_database_engine(database_url)
    return engine, make_session_factory(engine)


def test_q1_replay_hashes_match_across_independent_complete_sessions(
    tmp_path: Path,
) -> None:
    engine_a, factory_a = _new_database(tmp_path / "replay-a.db")
    engine_b, factory_b = _new_database(tmp_path / "replay-b.db")
    try:
        _seed_q1_replay(factory_a)
        _seed_q1_replay(factory_b)

        first = replay_q1_run(factory_a, RUN_ID)
        second = replay_q1_run(factory_b, RUN_ID)

        assert first.passed is True
        assert second.passed is True
        assert first.mode == second.mode == "FULL_EVENT_REPLAY"
        assert first.result_hash == second.result_hash
        assert first.manifest == second.manifest
        for hash_stream in (
            "evaluation_anchor_hashes",
            "decision_hashes",
            "intent_hashes",
            "order_event_hashes",
            "fill_hashes",
            "state_hashes",
            "nav_hashes",
            "risk_episode_hashes",
            "risk_target_hashes",
            "risk_event_hashes",
            "cash_settlement_event_hashes",
            "daily_result_hashes",
            "matched_attribution_hashes",
        ):
            assert first.manifest[hash_stream]
            assert first.manifest[hash_stream] == second.manifest[hash_stream]
    finally:
        engine_a.dispose()
        engine_b.dispose()


def test_interrupted_session_retries_to_exactly_one_committed_result(
    sqlite_database: tuple[str, Engine, sessionmaker[Session]],
) -> None:
    _, _, factory = sqlite_database

    with (
        pytest.raises(RuntimeError, match="synthetic interruption"),
        factory.begin() as session,
    ):
        _seed_q1_replay_session(session)
        raise RuntimeError("synthetic interruption before commit")

    with factory() as session:
        assert session.get(RunRow, RUN_ID) is None

    _seed_q1_replay(factory)
    replay = replay_q1_run(factory, RUN_ID)

    assert replay.passed is True
    with factory() as session:
        assert session.scalar(
            select(func.count()).select_from(RunRow)
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(FillRow)
        ) == len(TRADED_ARMS)
        assert session.scalar(
            select(func.count()).select_from(OrderEventRow)
        ) == len(TRADED_ARMS) * 2
        assert session.scalar(
            select(func.count()).select_from(CashSettlementEventRow)
        ) == len(Q1ArmId) + len(TRADED_ARMS)
        assert session.scalar(
            select(func.count()).select_from(StrategyDailyResultRow)
        ) == len(Q1ArmId)
        assert session.scalar(
            select(func.count()).select_from(MatchedAttributionResultRow)
        ) == 2


def test_empty_q1_stream_is_not_reported_as_full_replay(
    sqlite_database: tuple[str, Engine, sessionmaker[Session]],
) -> None:
    _, _, factory = sqlite_database
    with factory.begin() as session:
        session.add(
            RunRow(
                run_id=RUN_ID,
                mode="PAPER",
                experiment_version=ALGORITHM_VERSION,
                config_manifest_hash=CONFIG_HASH,
                code_commit=CODE_VERSION,
                started_at=OPEN_AT,
                ended_at=None,
                status="RUNNING",
                result_manifest={"real_order_routing": False},
                result_hash=None,
            )
        )

    replay = replay_q1_run(factory, RUN_ID)

    assert replay.passed is False
    assert replay.mode == "INCOMPLETE_EVENT_STREAM"
    assert replay.checks["complete_session_record_set_present"] is False
    assert replay.checks["initial_state_economics_valid"] is False


@pytest.mark.parametrize(
    ("seed_kwargs", "failed_check"),
    (
        ({"tamper_fill_hash": True}, "fill_hashes_valid"),
        ({"tamper_intent_hash": True}, "intent_hashes_valid"),
        ({"tamper_nav_economics": True}, "nav_economics_reconstructed"),
        ({"tamper_daily_hash": True}, "daily_result_hashes_valid"),
        ({"orphan_risk_target": True}, "typed_risk_targets_valid"),
    ),
)
def test_q1_replay_detects_cross_stream_inconsistency(
    sqlite_database: tuple[str, Engine, sessionmaker[Session]],
    seed_kwargs: dict[str, bool],
    failed_check: str,
) -> None:
    _, _, factory = sqlite_database
    _seed_q1_replay(factory, **seed_kwargs)

    replay = replay_q1_run(factory, RUN_ID)

    assert replay.passed is False
    assert replay.mode == "FULL_EVENT_REPLAY"
    assert replay.checks[failed_check] is False


def test_fill_event_and_cash_commission_economics_are_reconstructed(
    sqlite_database: tuple[str, Engine, sessionmaker[Session]],
) -> None:
    _, _, factory = sqlite_database
    _seed_q1_replay(factory)

    replay = replay_q1_run(factory, RUN_ID)

    assert replay.checks["order_state_machine_valid"] is True
    assert replay.checks["fill_order_event_economics_valid"] is True
    assert replay.checks["cash_settlement_economics_valid"] is True
    assert replay.checks["arm_states_reconstructed"] is True
    assert replay.checks["nav_economics_reconstructed"] is True
