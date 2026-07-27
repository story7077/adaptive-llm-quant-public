from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import ROUND_DOWN, Decimal
from itertools import pairwise
from typing import Any, cast
from zoneinfo import ZoneInfo

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session, sessionmaker

from trading.control.service import ControlPlaneService
from trading.data.alpaca import FEED, PROVIDER
from trading.data.market_repository import MarketDataRepository
from trading.domain.contracts import (
    Fill,
    OrderIntent,
    PortfolioDecision,
    model_payload,
)
from trading.domain.enums import OrderSide
from trading.domain.hashing import canonical_hash, stable_id
from trading.domain.time import require_aware_utc
from trading.execution.live_paper import (
    ExecutableQuoteNotFound,
    LivePaperExecutionService,
    QuoteDrivenFill,
)
from trading.execution.paper import PaperBroker
from trading.experiments.arms import ArmState
from trading.ledger.journal import fill_entry
from trading.llm.policy_compiler import PolicyState
from trading.persistence.models import (
    ArmStateSnapshotRow,
    FillRow,
    ForwardStrategyCandidateRow,
    LedgerPostingRow,
    LedgerTransactionRow,
    MarketBarRow,
    NavSnapshotRow,
    OrderIntentRow,
    PaperBootstrapCompletionRow,
    PaperCycleEffectRow,
    PaperCycleRow,
    PaperExecutionAttemptRow,
    PolicyVersionRow,
    PortfolioDecisionRow,
    RiskDecisionRow,
    RunRow,
    ShadowArmRow,
)
from trading.portfolio.forward import (
    FORWARD_ORDER_ARMS,
    ForwardPortfolioError,
    apply_core_rebalance_band,
    build_core_forecast,
    target_for_arm,
)
from trading.risk.forward import (
    DecisionQuote,
    ForwardLossGuard,
    ForwardOrderPlan,
    ForwardRiskConfig,
    ForwardRiskError,
    build_forward_order_plan,
    evaluate_forward_loss_guard,
    resolve_forced_reduction_targets,
)
from trading.runtime.paper import (
    PaperBootstrapNotReady,
    PaperRuntimeError,
    build_precise_nav,
)
from trading.settings import ConfigBundle

NEW_YORK = ZoneInfo("America/New_York")
EFFECT_DECISION = "FORWARD_TRADE_DECISION"
EFFECT_EXECUTION = "FORWARD_EXECUTION"


class ForwardPaperConflict(PaperRuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PendingOrder:
    row: OrderIntentRow
    intent: OrderIntent
    remaining_quantity: Decimal
    cumulative_notional_usd: Decimal
    cumulative_commission_usd: Decimal
    observed_after: datetime


@dataclass(frozen=True, slots=True)
class PreparedFill:
    pending: PendingOrder
    driven: QuoteDrivenFill
    state_before_sequence: int
    state_after: ArmState


class ForwardPaperTradingService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        config: ConfigBundle,
        max_quote_age_seconds: int,
    ) -> None:
        if max_quote_age_seconds <= 0:
            raise ValueError("max_quote_age_seconds must be positive")
        self._session_factory = session_factory
        self._config = config
        self._market = MarketDataRepository(session_factory)
        self._control = ControlPlaneService(session_factory)
        self._max_quote_age_seconds = max_quote_age_seconds

        forward = config.get("forward-paper.yaml")
        costs = config.get("costs.yaml")
        execution = forward["execution"]
        commission = costs["commission"]
        cost_execution = costs["execution"]
        self._broker = PaperBroker(
            execution_scenario_id=str(execution["execution_scenario_id"]),
            commission_rate=Decimal(str(commission["us_equity_rate"])),
            commission_waiver_threshold_usd=Decimal(
                str(commission["waive_if_order_total_usd_lte"])
            ),
            half_spread_bps=Decimal(
                str(cost_execution["conservative_half_spread_bps"])
            ),
            delay_penalty_bps=Decimal(str(cost_execution["delay_penalty_bps"])),
        )
        self._live_execution = LivePaperExecutionService(
            self._market,
            self._broker,
            max_quote_age_seconds=max_quote_age_seconds,
        )
        self._risk_config = _forward_risk_config(config)
        self._forward_config = forward
        self._order_enabled_arms = tuple(
            str(item) for item in forward["order_enabled_arms"]
        )
        self._loss_control_arms = frozenset(
            str(item)
            for item in forward["loss_controls"]["applies_to_arms"]
        )
        if tuple(self._order_enabled_arms) != FORWARD_ORDER_ARMS:
            raise ValueError(
                "forward-paper.yaml order_enabled_arms differs from the code contract"
            )
        if not self._loss_control_arms <= set(self._order_enabled_arms):
            raise ValueError("Loss-control arms must be forward order-enabled")

    def decide(
        self,
        cycle: PaperCycleRow,
        *,
        run_id: str,
        data_available_cutoff: datetime,
        created_at: datetime,
        policy_change_only: bool = False,
        loss_trigger_only: bool = False,
    ) -> dict[str, Any]:
        if policy_change_only and loss_trigger_only:
            raise ValueError("A forward decision cannot have two special modes")
        cutoff = require_aware_utc(
            data_available_cutoff,
            "data_available_cutoff",
        )
        actual = require_aware_utc(created_at, "created_at")
        existing = self._effect(cycle.cycle_id, EFFECT_DECISION)
        if existing is not None:
            return existing
        if cutoff > _aware(cycle.scheduled_at):
            raise ForwardPaperConflict(
                "Forward decision cutoff must not exceed its scheduled decision time"
            )
        self._require_running_run(run_id)
        completion = self._completion(run_id)
        if completion is None:
            raise PaperBootstrapNotReady("Paper T0 is not established")

        baseline = self._forward_config["baseline_contract"]
        scheduled_clock = _aware(cycle.scheduled_at).astimezone(NEW_YORK).strftime(
            "%H:%M"
        )
        is_baseline_rebalance = scheduled_clock == str(
            baseline["rebalance_time_et"]
        )
        if (
            not is_baseline_rebalance
            and not policy_change_only
            and not loss_trigger_only
        ):
            output: dict[str, Any] = {
                "status": "NO_TRADE_OUTSIDE_BASELINE_REBALANCE",
                "scheduled_at": _iso(cycle.scheduled_at),
                "data_available_cutoff": _iso(cutoff),
                "orders_created": 0,
                "order_enabled_arms": list(self._order_enabled_arms),
            }
            return self._persist_effect_only(
                cycle,
                effect_kind=EFFECT_DECISION,
                run_id=run_id,
                input_manifest={
                    "run_id": run_id,
                    "cycle_id": cycle.cycle_id,
                    "scheduled_at": _iso(cycle.scheduled_at),
                    "cutoff": _iso(cutoff),
                    "config_manifest_hash": self._config.manifest_hash,
                },
                output=output,
                now=actual,
            )
        decision_arms = (
            self._order_enabled_arms
            if is_baseline_rebalance
            else ("B3-RISK",)
        )
        signal_data_as_of = cutoff
        policy_as_of = cutoff if is_baseline_rebalance else actual
        portfolio_and_quotes_as_of = actual

        valid_until = _regular_close(_aware(cycle.scheduled_at))
        if actual >= valid_until:
            output = {
                "status": "MISSED_REGULAR_EXECUTION_WINDOW",
                "scheduled_at": _iso(cycle.scheduled_at),
                "completed_at": _iso(actual),
                "valid_until": _iso(valid_until),
                "orders_created": 0,
            }
            return self._persist_effect_only(
                cycle,
                effect_kind=EFFECT_DECISION,
                run_id=run_id,
                input_manifest=output,
                output=output,
                now=actual,
            )

        states = self._latest_states(
            run_id,
            decision_arms,
            as_of=portfolio_and_quotes_as_of,
        )
        daily_rows = self._qqq_daily_rows(cutoff)
        closes = [float(row.close) for row in daily_rows]
        core = build_core_forecast(
            closes,
            version=str(baseline["version"]),
            lookback_sessions=int(baseline["vol_lookback_trading_days"]),
            target_annualized_vol=float(baseline["target_annualized_vol"]),
        )
        symbols = {
            symbol
            for state in states.values()
            for symbol, quantity in state.positions.items()
            if quantity != 0
        }
        symbols.add(str(baseline["core_symbol"]))
        quotes = self._quote_bundle(
            symbols,
            as_of=portfolio_and_quotes_as_of,
            require_connected=True,
        )
        policy = self._control.active_policy_state(
            arm_scope="B3-RISK",
            scope_id=run_id,
            active_at=policy_as_of,
        )
        session_open = _regular_open(_aware(cycle.scheduled_at))
        loss_guards: dict[str, ForwardLossGuard] = {}
        turnover: dict[str, tuple[Decimal, Decimal, Decimal]] = {}
        risk_context: dict[str, dict[str, Any]] = {}
        for arm_id, state in sorted(states.items()):
            session_open_nav = self._session_open_nav(
                run_id=run_id,
                arm_id=arm_id,
                session_open=session_open,
            )
            peak_nav = self._peak_nav(
                run_id=run_id,
                arm_id=arm_id,
                since=_aware(completion.common_mark_at),
                as_of=portfolio_and_quotes_as_of,
            )
            guard = evaluate_forward_loss_guard(
                state=state,
                quotes=quotes,
                session_open_nav_usd=session_open_nav,
                peak_nav_usd=peak_nav,
                config=self._risk_config,
            )
            buy_notional, sell_notional, unsettled_sale_proceeds = (
                self._today_turnover(
                    run_id=run_id,
                    arm_id=arm_id,
                    session_open=session_open,
                    as_of=portfolio_and_quotes_as_of,
                )
            )
            loss_guards[arm_id] = guard
            turnover[arm_id] = (
                buy_notional,
                sell_notional,
                unsettled_sale_proceeds,
            )
            risk_context[arm_id] = {
                **guard.as_payload(),
                "loss_controls_applied": arm_id in self._loss_control_arms,
                "today_buy_notional_usd": format(buy_notional, "f"),
                "today_sell_notional_usd": format(sell_notional, "f"),
                "unsettled_sale_proceeds_usd": format(
                    unsettled_sale_proceeds,
                    "f",
                ),
            }
        input_manifest: dict[str, Any] = {
            "run_id": run_id,
            "cycle_id": cycle.cycle_id,
            "decision_time": _iso(cycle.scheduled_at),
            "data_available_cutoff": _iso(cutoff),
            "signal_data_as_of": _iso(signal_data_as_of),
            "policy_as_of": _iso(policy_as_of),
            "portfolio_and_quotes_as_of": _iso(
                portfolio_and_quotes_as_of
            ),
            "created_at": _iso(actual),
            "config_manifest_hash": self._config.manifest_hash,
            "states": {
                arm_id: state.as_payload() for arm_id, state in sorted(states.items())
            },
            "quote_ids": {
                symbol: quote.quote_id for symbol, quote in sorted(quotes.items())
            },
            "daily_bar_ids": [row.bar_id for row in daily_rows],
            "core_forecast": {
                "version": core.version,
                "observations": core.observations,
                "annualized_vol": core.annualized_vol,
                "qqq_weight": core.qqq_weight,
            },
            "b3_risk_policy": policy.as_payload(),
            "risk_context": risk_context,
        }
        if loss_trigger_only and not any(
            guard.block_new_entries for guard in loss_guards.values()
        ):
            output = {
                "status": "NO_B3_LOSS_TRIGGER",
                "decision_kind": "B3_LOSS_GUARD_CHECK",
                "decision_time": _iso(cycle.scheduled_at),
                "data_available_cutoff": _iso(cutoff),
                "portfolio_and_quotes_as_of": _iso(
                    portfolio_and_quotes_as_of
                ),
                "completed_at": _iso(actual),
                "orders_created": 0,
                "loss_guards": risk_context,
                "real_order_routing": False,
            }
            return self._persist_effect_only(
                cycle,
                effect_kind=EFFECT_DECISION,
                run_id=run_id,
                input_manifest=input_manifest,
                output=output,
                now=actual,
            )
        input_hash = canonical_hash(input_manifest)
        plans: dict[str, ForwardOrderPlan] = {}
        for arm_id, state in sorted(states.items()):
            target = target_for_arm(
                arm_id,
                core=core,
                policy=policy if arm_id == "B3-RISK" else PolicyState.default(arm_id),
            )
            if is_baseline_rebalance:
                target = apply_core_rebalance_band(
                    target,
                    cash_usd=state.cash_usd,
                    positions=state.positions,
                    quotes=quotes,
                    min_weight_delta=float(
                        baseline["min_rebalance_weight_delta"]
                    ),
                )
            buy_notional, sell_notional, unsettled_sale_proceeds = turnover[
                arm_id
            ]
            guard = loss_guards[arm_id]
            loss_controls_applied = arm_id in self._loss_control_arms
            latched_targets = (
                self._latched_forced_targets(
                    run_id=run_id,
                    arm_id=arm_id,
                    as_of=actual,
                )
                if (
                    loss_controls_applied
                    and guard.state in {"HARD_STOP", "FORCE_REDUCE"}
                )
                else None
            )
            forced_targets = resolve_forced_reduction_targets(
                guard=guard,
                latched_targets=latched_targets,
                state=state,
                quotes=quotes,
                config=self._risk_config,
                loss_controls_applied=loss_controls_applied,
            )
            plans[arm_id] = build_forward_order_plan(
                run_id=run_id,
                cycle_id=cycle.cycle_id,
                state=state,
                target=target,
                quotes=quotes,
                decision_time=_aware(cycle.scheduled_at),
                intent_created_at=actual,
                valid_until=valid_until,
                input_snapshot_hash=input_hash,
                session_open_nav_usd=guard.session_open_nav_usd,
                today_buy_notional_usd=buy_notional,
                today_sell_notional_usd=sell_notional,
                unsettled_sale_proceeds_usd=unsettled_sale_proceeds,
                config=self._risk_config,
                loss_state=(
                    guard.state if loss_controls_applied else "BENCHMARK_CONTROL"
                ),
                block_new_entries=(
                    (
                        guard.block_new_entries
                        if loss_controls_applied
                        else False
                    )
                    or policy_change_only
                    or loss_trigger_only
                ),
                forced_sell_budget_usd=(
                    guard.forced_sell_budget_usd
                    if loss_controls_applied and forced_targets is None
                    else None
                ),
                forced_target_quantities=forced_targets,
                allow_transition_sells=not loss_trigger_only,
            )
        output = {
            "status": (
                "FORWARD_BASELINE_DECISION_COMMITTED"
                if is_baseline_rebalance
                else (
                    "FORWARD_B3_LOSS_GUARD_COMMITTED"
                    if loss_trigger_only
                    else "FORWARD_AI_RISK_REDUCTION_COMMITTED"
                )
            ),
            "decision_kind": (
                "BASELINE_REBALANCE"
                if is_baseline_rebalance
                else (
                    "B3_LOSS_GUARD_REDUCE_ONLY"
                    if loss_trigger_only
                    else "AI_POLICY_CHANGE_REDUCE_ONLY"
                )
            ),
            "decision_time": _iso(cycle.scheduled_at),
            "data_available_cutoff": _iso(cutoff),
            "signal_data_as_of": _iso(signal_data_as_of),
            "policy_as_of": _iso(policy_as_of),
            "portfolio_and_quotes_as_of": _iso(
                portfolio_and_quotes_as_of
            ),
            "completed_at": _iso(actual),
            "baseline_version": str(baseline["version"]),
            "core_forecast": {
                "annualized_vol": core.annualized_vol,
                "qqq_weight": core.qqq_weight,
                "observations": core.observations,
            },
            "order_enabled_arms": list(decision_arms),
            "orders_created": sum(len(plan.intents) for plan in plans.values()),
            "arms": {
                arm_id: {
                    "portfolio_decision_id": plan.portfolio_decision.portfolio_decision_id,
                    "risk_decision_id": plan.risk_decision.risk_decision_id,
                    "risk_approved": plan.risk_decision.approved,
                    "orders_created": len(plan.intents),
                    "order_intent_ids": [
                        intent.order_intent_id for intent in plan.intents
                    ],
                    "loss_guard": risk_context[arm_id],
                    "diagnostics": plan.diagnostics,
                }
                for arm_id, plan in sorted(plans.items())
            },
            "strategy_candidates": (
                {
                    strategy_id: "BLOCKED"
                    for strategy_id in self._forward_config[
                        "strategy_candidates"
                    ]["strategies"]
                }
                if is_baseline_rebalance
                else {}
            ),
            "real_order_routing": False,
        }
        return self._persist_decision(
            cycle=cycle,
            run_id=run_id,
            cutoff=cutoff,
            policy_as_of=policy_as_of,
            actual=actual,
            input_manifest=input_manifest,
            plans=plans,
            quotes=quotes,
            valid_until=valid_until,
            persist_strategy_candidates=is_baseline_rebalance,
            output=output,
        )

    def execute(
        self,
        cycle: PaperCycleRow,
        *,
        run_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        instant = require_aware_utc(now, "now")
        existing = self._effect(cycle.cycle_id, EFFECT_EXECUTION)
        if existing is not None:
            return existing
        self._require_running_run(run_id)
        if not _regular_session_time(instant):
            output = {
                "status": "MARKET_CLOSED_NO_EXECUTION",
                "executed_at": _iso(instant),
                "fills_created": 0,
                "real_order_routing": False,
            }
            return self._persist_effect_only(
                cycle,
                effect_kind=EFFECT_EXECUTION,
                run_id=run_id,
                input_manifest=output,
                output=output,
                now=instant,
            )

        pending = self._pending_orders(run_id=run_id, as_of=instant)
        input_manifest: dict[str, Any] = {
            "run_id": run_id,
            "cycle_id": cycle.cycle_id,
            "as_of": _iso(instant),
            "pending": [
                {
                    "order_intent_id": item.intent.order_intent_id,
                    "remaining_quantity": format(item.remaining_quantity, "f"),
                    "cumulative_notional_usd": format(
                        item.cumulative_notional_usd, "f"
                    ),
                    "cumulative_commission_usd": format(
                        item.cumulative_commission_usd, "f"
                    ),
                    "observed_after": _iso(item.observed_after),
                }
                for item in pending
            ],
            "config_manifest_hash": self._config.manifest_hash,
        }
        if not pending:
            output: dict[str, Any] = {
                "status": "NO_PENDING_ORDERS",
                "executed_at": _iso(instant),
                "fills_created": 0,
                "attempts": [],
                "real_order_routing": False,
            }
            return self._persist_effect_only(
                cycle,
                effect_kind=EFFECT_EXECUTION,
                run_id=run_id,
                input_manifest=input_manifest,
                output=output,
                now=instant,
            )

        initial_states = self._latest_states(
            run_id,
            tuple(sorted({item.intent.arm_id for item in pending})),
            as_of=instant,
        )
        completion = self._completion(run_id)
        if completion is None:
            raise PaperBootstrapNotReady("Paper T0 is not established")
        session_open = _regular_open(instant)
        execution_loss_guards: dict[str, ForwardLossGuard] = {}
        for arm_id, state in sorted(initial_states.items()):
            symbols = {
                symbol
                for symbol, quantity in state.positions.items()
                if quantity != 0
            }
            quotes = self._quote_bundle(
                symbols,
                as_of=instant,
                require_connected=True,
            )
            guard = evaluate_forward_loss_guard(
                state=state,
                quotes=quotes,
                session_open_nav_usd=self._session_open_nav(
                    run_id=run_id,
                    arm_id=arm_id,
                    session_open=session_open,
                ),
                peak_nav_usd=self._peak_nav(
                    run_id=run_id,
                    arm_id=arm_id,
                    since=_aware(completion.common_mark_at),
                    as_of=instant,
                ),
                config=self._risk_config,
            )
            execution_loss_guards[arm_id] = guard
        input_manifest["execution_loss_guards"] = {
            arm_id: {
                **guard.as_payload(),
                "loss_controls_applied": arm_id in self._loss_control_arms,
            }
            for arm_id, guard in sorted(execution_loss_guards.items())
        }
        working_states = dict(initial_states)
        prepared: list[PreparedFill] = []
        attempt_payloads: list[dict[str, Any]] = []
        for item in sorted(
            pending,
            key=lambda order: (
                0 if order.intent.side is OrderSide.SELL else 1,
                self._execution_symbol_priority(order.intent.symbol),
                order.intent.arm_id,
                order.intent.symbol,
                order.intent.order_intent_id,
            ),
        ):
            state = working_states[item.intent.arm_id]
            loss_guard = execution_loss_guards[item.intent.arm_id]
            if (
                item.intent.arm_id in self._loss_control_arms
                and item.intent.side is OrderSide.BUY
                and loss_guard.block_new_entries
            ):
                attempt_payloads.append(
                    _attempt_payload(
                        item,
                        status="LOSS_GUARD_BLOCKED_PENDING_BUY",
                        quote_id=None,
                        detail=loss_guard.state,
                    )
                )
                continue
            adv_cap = self._adv_fill_cap(
                item.intent.symbol,
                run_id=run_id,
                arm_id=item.intent.arm_id,
                as_of=instant,
            )
            if adv_cap is None or adv_cap <= 0:
                attempt_payloads.append(
                    _attempt_payload(
                        item,
                        status="WAITING_FOR_20D_IEX_ADV",
                        quote_id=None,
                    )
                )
                continue
            max_fill_quantity = min(item.remaining_quantity, adv_cap)
            if item.intent.side is OrderSide.SELL:
                max_fill_quantity = min(
                    max_fill_quantity,
                    state.positions.get(item.intent.symbol, Decimal("0")),
                )
            if max_fill_quantity <= 0:
                attempt_payloads.append(
                    _attempt_payload(
                        item,
                        status="NO_EXECUTABLE_REMAINING_QUANTITY",
                        quote_id=None,
                    )
                )
                continue
            try:
                driven = self._live_execution.fill_market_order(
                    item.intent,
                    effective_at=instant,
                    observed_after=item.observed_after,
                    remaining_quantity=item.remaining_quantity,
                    max_spread_bps=self._max_spread_bps(item.intent.symbol),
                    participation_fraction=Decimal(
                        str(
                            self._forward_config["execution"][
                                "displayed_side_participation_fraction"
                            ]
                        )
                    ),
                    max_fill_quantity=max_fill_quantity,
                    cumulative_order_notional_before=item.cumulative_notional_usd,
                    commission_charged_before=item.cumulative_commission_usd,
                )
            except ExecutableQuoteNotFound:
                attempt_payloads.append(
                    _attempt_payload(
                        item,
                        status="WAITING_FOR_POST_INTENT_EXECUTABLE_QUOTE",
                        quote_id=None,
                    )
                )
                continue
            if not self._within_decision_price_guard(item.row, driven.fill):
                attempt_payloads.append(
                    _attempt_payload(
                        item,
                        status="DECISION_PRICE_GUARD_BLOCKED",
                        quote_id=driven.quote_id,
                    )
                )
                continue
            try:
                next_state = state.apply_fill(driven.fill)
            except ValueError as exc:
                attempt_payloads.append(
                    _attempt_payload(
                        item,
                        status=f"STATE_GUARD:{type(exc).__name__}",
                        quote_id=driven.quote_id,
                        detail=str(exc),
                    )
                )
                continue
            prepared.append(
                PreparedFill(
                    pending=item,
                    driven=driven,
                    state_before_sequence=state.sequence,
                    state_after=next_state,
                )
            )
            working_states[item.intent.arm_id] = next_state
            attempt_payloads.append(
                _attempt_payload(
                    item,
                    status="FILLED"
                    if driven.fill.quantity == item.remaining_quantity
                    else "PARTIALLY_FILLED",
                    quote_id=driven.quote_id,
                    fill=driven.fill,
                )
            )

        nav_inputs: dict[
            str,
            tuple[ArmState, dict[str, DecisionQuote]],
        ] = {}
        for arm_id in sorted({item.pending.intent.arm_id for item in prepared}):
            state = working_states[arm_id]
            symbols = {
                symbol
                for symbol, quantity in state.positions.items()
                if quantity != 0
            }
            nav_inputs[arm_id] = (
                state,
                self._quote_bundle(
                    symbols,
                    as_of=instant,
                    require_connected=True,
                ),
            )
        output = {
            "status": (
                "FORWARD_FILLS_COMMITTED" if prepared else "NO_EXECUTABLE_FILLS"
            ),
            "executed_at": _iso(instant),
            "fills_created": len(prepared),
            "fill_ids": [item.driven.fill.fill_id for item in prepared],
            "attempts": attempt_payloads,
            "real_order_routing": False,
        }
        return self._persist_execution(
            cycle=cycle,
            run_id=run_id,
            now=instant,
            input_manifest=input_manifest,
            pending=pending,
            initial_states=initial_states,
            prepared=prepared,
            nav_inputs=nav_inputs,
            attempt_payloads=attempt_payloads,
            output=output,
        )

    def _persist_decision(
        self,
        *,
        cycle: PaperCycleRow,
        run_id: str,
        cutoff: datetime,
        policy_as_of: datetime,
        actual: datetime,
        input_manifest: dict[str, Any],
        plans: dict[str, ForwardOrderPlan],
        quotes: dict[str, DecisionQuote],
        valid_until: datetime,
        persist_strategy_candidates: bool,
        output: dict[str, Any],
    ) -> dict[str, Any]:
        input_hash = canonical_hash(input_manifest)
        with self._session_factory.begin() as session:
            self._lock_cycle(session, cycle, now=actual)
            previous = self._effect_in_session(session, cycle.cycle_id, EFFECT_DECISION)
            if previous is not None:
                return previous
            for arm_id, plan in sorted(plans.items()):
                expected_sequence = self._input_sequence(plan)
                current = self._lock_arm_state(
                    session,
                    run_id=run_id,
                    arm_id=arm_id,
                )
                if current.sequence != expected_sequence:
                    raise ForwardPaperConflict(
                        f"Arm {arm_id} changed from sequence "
                        f"{expected_sequence} to {current.sequence}"
                    )
                if arm_id == "B3-RISK":
                    latest_version = self._latest_policy_version(
                        session,
                        run_id=run_id,
                        arm_id=arm_id,
                        as_of=policy_as_of,
                    )
                    if latest_version != plan.portfolio_decision.policy_version:
                        raise ForwardPaperConflict(
                            "B3-RISK policy changed during forward decision"
                        )
                portfolio = plan.portfolio_decision
                session.add(
                    PortfolioDecisionRow(
                        portfolio_decision_id=portfolio.portfolio_decision_id,
                        run_id=run_id,
                        arm_id=arm_id,
                        source_cycle_id=cycle.cycle_id,
                        input_state_sequence=current.sequence,
                        decision_time=portfolio.decision_time,
                        payload_json=model_payload(portfolio),
                        decision_hash=canonical_hash(portfolio),
                    )
                )
                risk = plan.risk_decision
                session.add(
                    RiskDecisionRow(
                        risk_decision_id=risk.risk_decision_id,
                        portfolio_decision_id=risk.portfolio_decision_id,
                        source_cycle_id=cycle.cycle_id,
                        input_state_sequence=current.sequence,
                        approved=risk.approved,
                        payload_json=model_payload(risk),
                    )
                )
                for intent in plan.intents:
                    decision_quote = quotes[intent.symbol]
                    session.add(
                        OrderIntentRow(
                            order_intent_id=intent.order_intent_id,
                            run_id=run_id,
                            arm_id=arm_id,
                            source_cycle_id=cycle.cycle_id,
                            input_state_sequence=current.sequence,
                            symbol=intent.symbol,
                            side=intent.side.value,
                            quantity=intent.quantity,
                            created_at=intent.created_at,
                            valid_until=valid_until,
                            decision_quote_id=decision_quote.quote_id,
                            decision_reference_price=decision_quote.midpoint,
                            idempotency_key=intent.idempotency_key,
                            payload_json=model_payload(intent),
                            intent_hash=canonical_hash(intent),
                        )
                    )
            if persist_strategy_candidates:
                self._persist_strategy_candidates(
                    session,
                    run_id=run_id,
                    cycle_id=cycle.cycle_id,
                    decision_time=_aware(cycle.scheduled_at),
                    cutoff=cutoff,
                    created_at=actual,
                    input_hash=input_hash,
                )
            self._add_effect(
                session,
                cycle_id=cycle.cycle_id,
                run_id=run_id,
                effect_kind=EFFECT_DECISION,
                input_manifest_hash=input_hash,
                output=output,
                created_at=actual,
            )
        return output

    def _persist_execution(
        self,
        *,
        cycle: PaperCycleRow,
        run_id: str,
        now: datetime,
        input_manifest: dict[str, Any],
        pending: list[PendingOrder],
        initial_states: dict[str, ArmState],
        prepared: list[PreparedFill],
        nav_inputs: dict[str, tuple[ArmState, dict[str, DecisionQuote]]],
        attempt_payloads: list[dict[str, Any]],
        output: dict[str, Any],
    ) -> dict[str, Any]:
        input_hash = canonical_hash(input_manifest)
        with self._session_factory.begin() as session:
            self._lock_cycle(session, cycle, now=now)
            previous = self._effect_in_session(
                session,
                cycle.cycle_id,
                EFFECT_EXECUTION,
            )
            if previous is not None:
                return previous
            for arm_id, expected in sorted(initial_states.items()):
                current = self._lock_arm_state(
                    session,
                    run_id=run_id,
                    arm_id=arm_id,
                )
                if current.sequence != expected.sequence:
                    raise ForwardPaperConflict(
                        f"Arm {arm_id} changed during execution preparation"
                    )
            for pending_item in pending:
                latest_decision_id = self._latest_forward_decision_id(
                    session,
                    run_id=run_id,
                    arm_id=pending_item.intent.arm_id,
                )
                if (
                    latest_decision_id
                    != pending_item.intent.portfolio_decision_id
                ):
                    raise ForwardPaperConflict(
                        "Pending order was superseded during execution preparation"
                    )
                terminal_attempt = session.scalar(
                    select(PaperExecutionAttemptRow.attempt_id)
                    .where(
                        PaperExecutionAttemptRow.order_intent_id
                        == pending_item.intent.order_intent_id,
                        PaperExecutionAttemptRow.status.in_(
                            (
                                "LOSS_GUARD_BLOCKED_PENDING_BUY",
                                "SUPERSEDED_BY_NEWER_PORTFOLIO_DECISION",
                            )
                        ),
                    )
                    .limit(1)
                )
                if terminal_attempt is not None:
                    raise ForwardPaperConflict(
                        "Pending order was terminally canceled during "
                        "execution preparation"
                    )

            for item in prepared:
                fill = item.driven.fill
                session.add(
                    FillRow(
                        fill_id=fill.fill_id,
                        order_intent_id=fill.order_intent_id,
                        run_id=run_id,
                        arm_id=fill.arm_id,
                        source_cycle_id=cycle.cycle_id,
                        quote_id=item.driven.quote_id,
                        quote_event_time=item.driven.quote_event_time,
                        quote_available_at=item.driven.quote_available_at,
                        symbol=fill.symbol,
                        side=fill.side.value,
                        quantity=fill.quantity,
                        price=fill.price,
                        commission_usd=fill.commission_usd,
                        execution_scenario_id=fill.execution_scenario_id,
                        fill_hash=canonical_hash(fill),
                        effective_at=fill.effective_at,
                        payload_json=model_payload(fill),
                    )
                )
                entry = fill_entry(fill)
                session.add(
                    LedgerTransactionRow(
                        ledger_transaction_id=entry.transaction.ledger_transaction_id,
                        run_id=run_id,
                        arm_id=fill.arm_id,
                        source_id=entry.transaction.source_id,
                        effective_at=entry.transaction.effective_at,
                        payload_json=model_payload(entry.transaction),
                    )
                )
                session.flush()
                for posting in entry.postings:
                    session.add(
                        LedgerPostingRow(
                            posting_id=posting.posting_id,
                            ledger_transaction_id=posting.ledger_transaction_id,
                            account_code=posting.account_code,
                            asset_code=posting.asset_code,
                            quantity_delta=posting.quantity_delta,
                            usd_value_delta=posting.usd_value_delta,
                            payload_json=model_payload(posting),
                        )
                    )
                state = item.state_after
                session.add(
                    ArmStateSnapshotRow(
                        arm_state_snapshot_id=stable_id(
                            "armstate",
                            run_id,
                            state.arm_id,
                            state.sequence,
                        ),
                        run_id=run_id,
                        arm_id=state.arm_id,
                        sequence=state.sequence,
                        source_cycle_id=cycle.cycle_id,
                        state_hash=canonical_hash(state.as_payload()),
                        payload_json=state.as_payload(),
                        created_at=now,
                    )
                )

            for arm_id, (state, quote_bundle) in sorted(nav_inputs.items()):
                prices = {
                    symbol: quote.midpoint
                    for symbol, quote in quote_bundle.items()
                }
                quote_ids = {
                    symbol: quote.quote_id
                    for symbol, quote in quote_bundle.items()
                }
                snapshot = build_precise_nav(
                    run_id=run_id,
                    arm_id=arm_id,
                    as_of=now,
                    cash_usd=state.cash_usd,
                    positions={
                        symbol: quantity
                        for symbol, quantity in state.positions.items()
                        if quantity != 0
                    },
                    prices=prices,
                    quote_ids=quote_ids,
                    snapshot_scope=f"{cycle.cycle_id}:{arm_id}",
                )
                session.add(
                    NavSnapshotRow(
                        nav_snapshot_id=snapshot.nav_snapshot_id,
                        run_id=run_id,
                        arm_id=arm_id,
                        source_cycle_id=cycle.cycle_id,
                        quote_manifest_hash=snapshot.price_manifest_hash,
                        as_of=snapshot.as_of,
                        nav_usd=snapshot.nav_usd,
                        payload_json=model_payload(snapshot),
                    )
                )

            attempts_by_order = {
                str(item["order_intent_id"]): item for item in attempt_payloads
            }
            for pending_item in pending:
                payload = attempts_by_order.get(pending_item.intent.order_intent_id)
                if payload is None:
                    continue
                fill_quantity = Decimal(str(payload.get("fill_quantity", "0")))
                remaining_after = max(
                    Decimal("0"),
                    pending_item.remaining_quantity - fill_quantity,
                )
                cumulative_notional = pending_item.cumulative_notional_usd + Decimal(
                    str(payload.get("fill_notional_usd", "0"))
                )
                cumulative_commission = (
                    pending_item.cumulative_commission_usd
                    + Decimal(str(payload.get("commission_usd", "0")))
                )
                attempt_hash = canonical_hash(payload)
                session.add(
                    PaperExecutionAttemptRow(
                        attempt_id=stable_id(
                            "paper-execution-attempt",
                            cycle.cycle_id,
                            pending_item.intent.order_intent_id,
                        ),
                        cycle_id=cycle.cycle_id,
                        order_intent_id=pending_item.intent.order_intent_id,
                        quote_id=(
                            None
                            if payload.get("quote_id") is None
                            else str(payload["quote_id"])
                        ),
                        status=str(payload["status"]),
                        remaining_quantity_before=pending_item.remaining_quantity,
                        remaining_quantity_after=remaining_after,
                        cumulative_notional_usd=cumulative_notional,
                        cumulative_commission_usd=cumulative_commission,
                        attempt_hash=attempt_hash,
                        payload_json=payload,
                        created_at=now,
                    )
                )
            self._add_effect(
                session,
                cycle_id=cycle.cycle_id,
                run_id=run_id,
                effect_kind=EFFECT_EXECUTION,
                input_manifest_hash=input_hash,
                output=output,
                created_at=now,
            )
        return output

    def _persist_effect_only(
        self,
        cycle: PaperCycleRow,
        *,
        effect_kind: str,
        run_id: str,
        input_manifest: dict[str, Any],
        output: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        with self._session_factory.begin() as session:
            self._lock_cycle(session, cycle, now=now)
            previous = self._effect_in_session(
                session,
                cycle.cycle_id,
                effect_kind,
            )
            if previous is not None:
                return previous
            self._add_effect(
                session,
                cycle_id=cycle.cycle_id,
                run_id=run_id,
                effect_kind=effect_kind,
                input_manifest_hash=canonical_hash(input_manifest),
                output=output,
                created_at=now,
            )
        return output

    def _persist_strategy_candidates(
        self,
        session: Session,
        *,
        run_id: str,
        cycle_id: str,
        decision_time: datetime,
        cutoff: datetime,
        created_at: datetime,
        input_hash: str,
    ) -> None:
        registry = self._forward_config["strategy_candidates"]
        for strategy_id, raw in sorted(registry["strategies"].items()):
            version = str(raw["version"])
            payload = {
                "schema_version": str(registry["version"]),
                "strategy_id": strategy_id,
                "strategy_version": version,
                "decision_time": _iso(decision_time),
                "data_available_cutoff": _iso(cutoff),
                "status": "BLOCKED",
                "reason_code": str(raw["blocked_reason"]),
                "orders_created": 0,
                "promotion_required": True,
            }
            session.add(
                ForwardStrategyCandidateRow(
                    candidate_id=stable_id(
                        "forward-candidate",
                        run_id,
                        strategy_id,
                        version,
                        decision_time,
                    ),
                    run_id=run_id,
                    source_cycle_id=cycle_id,
                    strategy_id=strategy_id,
                    strategy_version=version,
                    decision_time=decision_time,
                    data_available_cutoff=cutoff,
                    status="BLOCKED",
                    reason_code=str(raw["blocked_reason"]),
                    input_manifest_hash=input_hash,
                    payload_json=payload,
                    created_at=created_at,
                )
            )

    def _pending_orders(
        self,
        *,
        run_id: str,
        as_of: datetime,
    ) -> list[PendingOrder]:
        with self._session_factory() as session:
            rows = list(
                session.scalars(
                    select(OrderIntentRow)
                    .where(
                        OrderIntentRow.run_id == run_id,
                        OrderIntentRow.source_cycle_id.is_not(None),
                        OrderIntentRow.created_at <= as_of,
                        OrderIntentRow.valid_until > as_of,
                    )
                    .order_by(
                        OrderIntentRow.created_at,
                        OrderIntentRow.arm_id,
                        OrderIntentRow.order_intent_id,
                    )
                )
            )
            if not rows:
                return []
            decision_rows = list(
                session.scalars(
                    select(PortfolioDecisionRow)
                    .where(
                        PortfolioDecisionRow.run_id == run_id,
                        PortfolioDecisionRow.source_cycle_id.is_not(None),
                    )
                )
            )
            decision_rows = [
                row
                for row in decision_rows
                if _aware(
                    PortfolioDecision.model_validate(
                        row.payload_json
                    ).created_at
                )
                <= as_of
            ]
            latest_decision_rows: dict[str, PortfolioDecisionRow] = {}
            for decision_row in decision_rows:
                current = latest_decision_rows.get(decision_row.arm_id)
                if (
                    current is None
                    or _portfolio_decision_sort_key(decision_row)
                    > _portfolio_decision_sort_key(current)
                ):
                    latest_decision_rows[decision_row.arm_id] = decision_row
            latest_decision_by_arm = {
                arm_id: row.portfolio_decision_id
                for arm_id, row in latest_decision_rows.items()
            }
            rows = [
                row
                for row in rows
                if OrderIntent.model_validate(
                    row.payload_json
                ).portfolio_decision_id
                == latest_decision_by_arm.get(row.arm_id)
            ]
            if not rows:
                return []
            order_ids = [row.order_intent_id for row in rows]
            terminal_attempt_order_ids = set(
                session.scalars(
                    select(PaperExecutionAttemptRow.order_intent_id).where(
                        PaperExecutionAttemptRow.order_intent_id.in_(order_ids),
                        PaperExecutionAttemptRow.status.in_(
                            (
                                "LOSS_GUARD_BLOCKED_PENDING_BUY",
                                "SUPERSEDED_BY_NEWER_PORTFOLIO_DECISION",
                            )
                        ),
                    )
                )
            )
            rows = [
                row
                for row in rows
                if row.order_intent_id not in terminal_attempt_order_ids
            ]
            if not rows:
                return []
            order_ids = [row.order_intent_id for row in rows]
            fill_rows = list(
                session.scalars(
                    select(FillRow)
                    .where(FillRow.order_intent_id.in_(order_ids))
                    .order_by(FillRow.effective_at, FillRow.fill_id)
                )
            )
        fills_by_order: defaultdict[str, list[FillRow]] = defaultdict(list)
        for row in fill_rows:
            fills_by_order[row.order_intent_id].append(row)
        pending: list[PendingOrder] = []
        for row in rows:
            intent = OrderIntent.model_validate(row.payload_json)
            existing = fills_by_order[intent.order_intent_id]
            fill_quantity = sum(
                (_fill_contract(item).quantity for item in existing),
                Decimal("0"),
            )
            remaining = intent.quantity - fill_quantity
            if remaining <= 0:
                continue
            cumulative_notional = sum(
                (
                    _fill_contract(item).quantity * _fill_contract(item).price
                    for item in existing
                ),
                Decimal("0"),
            )
            cumulative_commission = sum(
                (_fill_contract(item).commission_usd for item in existing),
                Decimal("0"),
            )
            observed_after = max(
                (
                    _aware(item.quote_available_at)
                    for item in existing
                    if item.quote_available_at is not None
                ),
                default=intent.created_at,
            )
            pending.append(
                PendingOrder(
                    row=row,
                    intent=intent,
                    remaining_quantity=remaining,
                    cumulative_notional_usd=cumulative_notional,
                    cumulative_commission_usd=cumulative_commission,
                    observed_after=observed_after,
                )
            )
        return pending

    def _latest_states(
        self,
        run_id: str,
        arm_ids: tuple[str, ...],
        *,
        as_of: datetime,
    ) -> dict[str, ArmState]:
        with self._session_factory() as session:
            rows = list(
                session.scalars(
                    select(ArmStateSnapshotRow)
                    .where(
                        ArmStateSnapshotRow.run_id == run_id,
                        ArmStateSnapshotRow.arm_id.in_(arm_ids),
                        ArmStateSnapshotRow.created_at <= as_of,
                    )
                    .order_by(
                        ArmStateSnapshotRow.arm_id,
                        ArmStateSnapshotRow.sequence.desc(),
                    )
                )
            )
        states: dict[str, ArmState] = {}
        for row in rows:
            states.setdefault(row.arm_id, ArmState.from_payload(row.payload_json))
        missing = sorted(set(arm_ids) - set(states))
        if missing:
            raise PaperBootstrapNotReady(f"Missing forward arm state: {missing}")
        return states

    def _quote_bundle(
        self,
        symbols: set[str],
        *,
        as_of: datetime,
        require_connected: bool,
    ) -> dict[str, DecisionQuote]:
        if not symbols:
            return {}
        if require_connected:
            status = self._market.status(provider=PROVIDER, feed=FEED)
            if status is None or status.state != "CONNECTED":
                raise ForwardRiskError("IEX stream is not CONNECTED")
        quotes: dict[str, DecisionQuote] = {}
        event_times: list[datetime] = []
        for symbol in sorted(symbols):
            row = self._market.latest_quote(
                provider=PROVIDER,
                feed=FEED,
                symbol=symbol,
                as_of=as_of,
            )
            if row is None:
                raise ForwardRiskError(f"Missing decision quote for {symbol}")
            event_time = _aware(row.event_time)
            age = (as_of - event_time).total_seconds()
            if age < 0 or age > self._max_quote_age_seconds:
                raise ForwardRiskError(f"Stale decision quote for {symbol}")
            if (
                row.bid_price <= 0
                or row.ask_price <= 0
                or row.ask_price < row.bid_price
                or row.bid_size_round_lots <= 0
                or row.ask_size_round_lots <= 0
            ):
                raise ForwardRiskError(f"Non-executable decision quote for {symbol}")
            quote = DecisionQuote(
                symbol=symbol,
                quote_id=row.quote_id,
                bid_price=row.bid_price,
                ask_price=row.ask_price,
                bid_size_round_lots=row.bid_size_round_lots,
                ask_size_round_lots=row.ask_size_round_lots,
                event_time=event_time,
                available_at=_aware(row.available_at),
            )
            if quote.spread_bps > self._max_spread_bps(symbol):
                raise ForwardRiskError(f"Decision spread is too wide for {symbol}")
            quotes[symbol] = quote
            event_times.append(event_time)
        max_skew = int(self._config.get("risk.yaml")["risk"].get(
            "max_quote_skew_seconds",
            20,
        ))
        if (
            event_times
            and (max(event_times) - min(event_times)).total_seconds() > max_skew
        ):
            raise ForwardRiskError("Decision quote bundle exceeds allowed skew")
        return quotes

    def _qqq_daily_rows(self, cutoff: datetime) -> list[MarketBarRow]:
        session_day_start = _session_day_start(cutoff)
        lookback = int(
            self._forward_config["baseline_contract"][
                "vol_lookback_trading_days"
            ]
        )
        with self._session_factory() as session:
            candidates = list(
                session.scalars(
                    select(MarketBarRow)
                    .where(
                        MarketBarRow.provider == PROVIDER,
                        MarketBarRow.feed == FEED,
                        MarketBarRow.symbol == "QQQ",
                        MarketBarRow.timeframe == "1Day",
                        MarketBarRow.payload_json["_adjustment"].as_string()
                        == "all",
                        MarketBarRow.payload_json[
                            "_dataset_version"
                        ].as_string()
                        == "alpaca_iex_adjusted_all_v1",
                        MarketBarRow.event_time < session_day_start,
                        MarketBarRow.available_at <= cutoff,
                    )
                    .order_by(
                        MarketBarRow.event_time.desc(),
                        MarketBarRow.available_at.desc(),
                        MarketBarRow.bar_id,
                    )
                    .limit(max(100, (lookback + 1) * 4))
                )
            )
        by_event: dict[datetime, MarketBarRow] = {}
        for row in candidates:
            by_event.setdefault(_aware(row.event_time), row)
        selected = sorted(by_event.values(), key=lambda row: _aware(row.event_time))[
            -(lookback + 1) :
        ]
        if len(selected) < lookback + 1:
            raise ForwardPortfolioError(
                f"QQQ daily PIT panel has only {len(selected)} rows"
            )
        event_times = [_aware(row.event_time) for row in selected]
        if session_day_start - event_times[-1] > timedelta(days=7):
            raise ForwardPortfolioError("QQQ daily PIT panel is stale")
        if any(
            later - earlier > timedelta(days=7)
            for earlier, later in pairwise(event_times)
        ):
            raise ForwardPortfolioError("QQQ daily PIT panel has a session gap")
        return selected

    def _session_open_nav(
        self,
        *,
        run_id: str,
        arm_id: str,
        session_open: datetime,
    ) -> Decimal:
        with self._session_factory() as session:
            row = session.scalar(
                select(NavSnapshotRow)
                .where(
                    NavSnapshotRow.run_id == run_id,
                    NavSnapshotRow.arm_id == arm_id,
                    NavSnapshotRow.as_of >= session_open,
                )
                .order_by(NavSnapshotRow.as_of)
                .limit(1)
            )
        if row is not None:
            return row.nav_usd
        raise ForwardRiskError(
            f"Missing session-open NAV reference for {arm_id}"
        )

    def _peak_nav(
        self,
        *,
        run_id: str,
        arm_id: str,
        since: datetime,
        as_of: datetime,
    ) -> Decimal:
        with self._session_factory() as session:
            value = session.scalar(
                select(func.max(NavSnapshotRow.nav_usd)).where(
                    NavSnapshotRow.run_id == run_id,
                    NavSnapshotRow.arm_id == arm_id,
                    NavSnapshotRow.as_of >= since,
                    NavSnapshotRow.as_of <= as_of,
                )
            )
        if value is None:
            raise ForwardRiskError(f"Missing peak NAV reference for {arm_id}")
        return Decimal(value)

    def _today_turnover(
        self,
        *,
        run_id: str,
        arm_id: str,
        session_open: datetime,
        as_of: datetime,
    ) -> tuple[Decimal, Decimal, Decimal]:
        with self._session_factory() as session:
            rows = list(
                session.scalars(
                    select(FillRow).where(
                        FillRow.run_id == run_id,
                        FillRow.arm_id == arm_id,
                        FillRow.effective_at >= session_open,
                        FillRow.effective_at <= as_of,
                    )
                )
            )
        buys = Decimal("0")
        sells = Decimal("0")
        unsettled_sale_proceeds = Decimal("0")
        for row in rows:
            fill = _fill_contract(row)
            notional = fill.quantity * fill.price
            if fill.side is OrderSide.BUY:
                buys += notional
            else:
                sells += notional
                unsettled_sale_proceeds += max(
                    Decimal("0"),
                    notional - fill.commission_usd,
                )
        return buys, sells, unsettled_sale_proceeds

    def _latched_forced_targets(
        self,
        *,
        run_id: str,
        arm_id: str,
        as_of: datetime,
    ) -> dict[str, Decimal] | None:
        with self._session_factory() as session:
            rows = list(
                session.scalars(
                    select(PaperCycleEffectRow)
                    .where(
                        PaperCycleEffectRow.run_id == run_id,
                        PaperCycleEffectRow.effect_kind == EFFECT_DECISION,
                        PaperCycleEffectRow.created_at <= as_of,
                    )
                    .order_by(
                        PaperCycleEffectRow.created_at.desc(),
                        PaperCycleEffectRow.effect_id.desc(),
                    )
                    .limit(100)
                )
            )
        for row in rows:
            payload = row.payload_json
            arms_value: object = payload.get("arms")
            if isinstance(arms_value, dict):
                arms = cast(dict[str, Any], arms_value)
                arm_value: object = arms.get(arm_id)
                if isinstance(arm_value, dict):
                    arm_payload = cast(dict[str, Any], arm_value)
                    guard_value: object = arm_payload.get("loss_guard")
                    if not isinstance(guard_value, dict):
                        continue
                    guard = cast(dict[str, Any], guard_value)
                    if "state" not in guard:
                        continue
                    if str(guard["state"]) not in {"HARD_STOP", "FORCE_REDUCE"}:
                        return None
                    diagnostics_value: object = arm_payload.get("diagnostics")
                    if not isinstance(diagnostics_value, dict):
                        return None
                    diagnostics = cast(dict[str, Any], diagnostics_value)
                    targets_value: object = diagnostics.get(
                        "forced_target_quantities"
                    )
                    if not isinstance(targets_value, dict):
                        return None
                    raw_targets = cast(dict[str, Any], targets_value)
                    return {
                        str(symbol): Decimal(str(quantity))
                        for symbol, quantity in raw_targets.items()
                    }
            loss_guards_value: object = payload.get("loss_guards")
            if isinstance(loss_guards_value, dict):
                loss_guards = cast(dict[str, Any], loss_guards_value)
                guard_value = loss_guards.get(arm_id)
                guard = (
                    cast(dict[str, Any], guard_value)
                    if isinstance(guard_value, dict)
                    else None
                )
                if (
                    guard is not None
                    and "state" in guard
                    and str(guard["state"])
                    not in {
                        "HARD_STOP",
                        "FORCE_REDUCE",
                    }
                ):
                    return None
        return None

    def _adv_fill_cap(
        self,
        symbol: str,
        *,
        run_id: str,
        arm_id: str,
        as_of: datetime,
    ) -> Decimal | None:
        session_day_start = _session_day_start(as_of)
        session_open = _regular_open(as_of)
        with self._session_factory() as session:
            rows = list(
                session.scalars(
                    select(MarketBarRow)
                    .where(
                        MarketBarRow.provider == PROVIDER,
                        MarketBarRow.feed == FEED,
                        MarketBarRow.symbol == symbol,
                        MarketBarRow.timeframe == "1Day",
                        MarketBarRow.payload_json["_adjustment"].as_string()
                        == "all",
                        MarketBarRow.payload_json[
                            "_dataset_version"
                        ].as_string()
                        == "alpaca_iex_adjusted_all_v1",
                        MarketBarRow.event_time < session_day_start,
                        MarketBarRow.available_at <= as_of,
                    )
                    .order_by(
                        MarketBarRow.event_time.desc(),
                        MarketBarRow.available_at.desc(),
                    )
                    .limit(80)
                )
            )
            consumed = session.scalar(
                select(func.coalesce(func.sum(FillRow.quantity), 0)).where(
                    FillRow.run_id == run_id,
                    FillRow.arm_id == arm_id,
                    FillRow.symbol == symbol,
                    FillRow.effective_at >= session_open,
                    FillRow.effective_at <= as_of,
                )
            )
        by_event: dict[datetime, Decimal] = {}
        for row in rows:
            by_event.setdefault(_aware(row.event_time), row.volume)
        volumes = list(by_event.values())[:20]
        if len(volumes) < 20:
            return None
        average = sum(volumes, Decimal("0")) / Decimal(len(volumes))
        fraction = Decimal(
            str(self._forward_config["execution"]["max_20d_iex_adv_fraction"])
        )
        precision = Decimal(
            str(self._forward_config["execution"]["quantity_precision"])
        )
        daily_cap = (average * fraction).quantize(
            precision,
            rounding=ROUND_DOWN,
        )
        consumed_quantity = Decimal("0") if consumed is None else Decimal(consumed)
        return max(
            Decimal("0"),
            daily_cap - consumed_quantity,
        ).quantize(precision, rounding=ROUND_DOWN)

    def _within_decision_price_guard(
        self,
        row: OrderIntentRow,
        fill: Fill,
    ) -> bool:
        reference = row.decision_reference_price
        if reference is None or reference <= 0:
            return False
        guard = Decimal(
            str(self._forward_config["execution"]["decision_price_guard_bps"])
        ) / Decimal("10000")
        if fill.side is OrderSide.BUY:
            return fill.price <= reference * (Decimal("1") + guard)
        return fill.price >= reference * (Decimal("1") - guard)

    def _max_spread_bps(self, symbol: str) -> Decimal:
        spread = self._config.get("risk.yaml")["risk"]["max_spread_bps"]
        return Decimal(str(spread.get(symbol, spread["default"])))

    def _execution_symbol_priority(self, symbol: str) -> int:
        if symbol in self._risk_config.leveraged_symbols:
            return 0
        if symbol in self._risk_config.semiconductor_symbols:
            return 1
        return 2

    def _require_running_run(self, run_id: str) -> None:
        with self._session_factory() as session:
            row = session.get(RunRow, run_id)
        if row is None:
            raise PaperRuntimeError(f"Unknown paper run: {run_id}")
        if row.mode != "PAPER":
            raise PaperRuntimeError(f"Run {run_id!r} is not PAPER")
        if row.status != "RUNNING":
            raise PaperBootstrapNotReady(
                f"Paper run {run_id!r} is not RUNNING ({row.status})"
            )

    def _completion(self, run_id: str) -> PaperBootstrapCompletionRow | None:
        with self._session_factory() as session:
            return session.scalar(
                select(PaperBootstrapCompletionRow).where(
                    PaperBootstrapCompletionRow.run_id == run_id
                )
            )

    def _lock_cycle(
        self,
        session: Session,
        cycle: PaperCycleRow,
        *,
        now: datetime,
    ) -> PaperCycleRow:
        statement = select(PaperCycleRow).where(
            PaperCycleRow.cycle_id == cycle.cycle_id
        )
        is_postgresql = (
            session.bind is not None
            and session.bind.dialect.name == "postgresql"
        )
        if is_postgresql:
            statement = statement.with_for_update()
        row = session.scalar(statement)
        if row is None:
            raise ForwardPaperConflict(f"Unknown cycle {cycle.cycle_id}")
        comparison_now = now
        if is_postgresql:
            database_now = session.scalar(select(func.clock_timestamp()))
            if database_now is None:
                raise ForwardPaperConflict("Database clock is unavailable")
            comparison_now = _aware(database_now)
        if (
            row.status != "RUNNING"
            or row.lease_owner is None
            or row.lease_owner != cycle.lease_owner
            or row.attempt_count != cycle.attempt_count
            or row.lease_expires_at is None
            or _aware(row.lease_expires_at) <= comparison_now
        ):
            raise ForwardPaperConflict(
                f"Cycle lease is not owned by attempt {cycle.attempt_count}"
            )
        return row

    def _lock_arm_state(
        self,
        session: Session,
        *,
        run_id: str,
        arm_id: str,
    ) -> ArmState:
        arm_statement = select(ShadowArmRow).where(
            ShadowArmRow.run_id == run_id,
            ShadowArmRow.arm_id == arm_id,
        )
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            arm_statement = arm_statement.with_for_update()
        if session.scalar(arm_statement) is None:
            raise ForwardPaperConflict(f"Missing shadow arm {arm_id}")
        row = session.scalar(
            select(ArmStateSnapshotRow)
            .where(
                ArmStateSnapshotRow.run_id == run_id,
                ArmStateSnapshotRow.arm_id == arm_id,
            )
            .order_by(desc(ArmStateSnapshotRow.sequence))
            .limit(1)
        )
        if row is None:
            raise ForwardPaperConflict(f"Missing arm state {arm_id}")
        return ArmState.from_payload(row.payload_json)

    @staticmethod
    def _latest_forward_decision_id(
        session: Session,
        *,
        run_id: str,
        arm_id: str,
    ) -> str | None:
        rows = list(
            session.scalars(
                select(PortfolioDecisionRow).where(
                    PortfolioDecisionRow.run_id == run_id,
                    PortfolioDecisionRow.arm_id == arm_id,
                    PortfolioDecisionRow.source_cycle_id.is_not(None),
                )
            )
        )
        if not rows:
            return None
        return max(rows, key=_portfolio_decision_sort_key).portfolio_decision_id

    @staticmethod
    def _latest_policy_version(
        session: Session,
        *,
        run_id: str,
        arm_id: str,
        as_of: datetime,
    ) -> int:
        row = session.scalar(
            select(PolicyVersionRow)
            .where(
                PolicyVersionRow.scope_id == run_id,
                PolicyVersionRow.arm_id == arm_id,
                PolicyVersionRow.created_at <= as_of,
            )
            .order_by(desc(PolicyVersionRow.version))
            .limit(1)
        )
        return 0 if row is None else row.version

    @staticmethod
    def _input_sequence(plan: ForwardOrderPlan) -> int:
        return plan.input_state_sequence

    def _effect(
        self,
        cycle_id: str,
        effect_kind: str,
    ) -> dict[str, Any] | None:
        with self._session_factory() as session:
            return self._effect_in_session(session, cycle_id, effect_kind)

    @staticmethod
    def _effect_in_session(
        session: Session,
        cycle_id: str,
        effect_kind: str,
    ) -> dict[str, Any] | None:
        row = session.scalar(
            select(PaperCycleEffectRow).where(
                PaperCycleEffectRow.cycle_id == cycle_id,
                PaperCycleEffectRow.effect_kind == effect_kind,
            )
        )
        return None if row is None else dict(row.payload_json)

    @staticmethod
    def _add_effect(
        session: Session,
        *,
        cycle_id: str,
        run_id: str,
        effect_kind: str,
        input_manifest_hash: str,
        output: dict[str, Any],
        created_at: datetime,
    ) -> None:
        session.add(
            PaperCycleEffectRow(
                effect_id=stable_id("paper-cycle-effect", cycle_id, effect_kind),
                cycle_id=cycle_id,
                run_id=run_id,
                effect_kind=effect_kind,
                input_manifest_hash=input_manifest_hash,
                output_manifest_hash=canonical_hash(output),
                payload_json=output,
                created_at=created_at,
            )
        )


def _portfolio_decision_sort_key(
    row: PortfolioDecisionRow,
) -> tuple[datetime, datetime, str]:
    decision = PortfolioDecision.model_validate(row.payload_json)
    return (
        _aware(row.decision_time),
        _aware(decision.created_at),
        row.portfolio_decision_id,
    )


def _forward_risk_config(config: ConfigBundle) -> ForwardRiskConfig:
    forward = config.get("forward-paper.yaml")
    risk = config.get("risk.yaml")
    portfolio = config.get("portfolio.yaml")
    costs = config.get("costs.yaml")
    universe = config.get("universe.yaml")
    transition = forward["transition"]
    baseline = forward["baseline_contract"]
    risk_values = risk["risk"]
    portfolio_values = portfolio["portfolio"]
    commission = costs["commission"]
    execution = costs["execution"]
    clusters = universe["clusters"]
    return ForwardRiskConfig(
        version=str(risk["version"]),
        transition_version=str(transition["version"]),
        core_version=str(baseline["version"]),
        max_one_way_daily_turnover=Decimal(
            str(transition["max_one_way_daily_turnover"])
        ),
        min_order_notional_usd=Decimal(
            str(transition["min_order_notional_usd"])
        ),
        buy_cash_reserve_fraction=Decimal(
            str(transition["buy_cash_reserve_fraction"])
        ),
        spend_unsettled_sale_proceeds=bool(
            transition["spend_unsettled_sale_proceeds"]
        ),
        max_single_symbol_weight=Decimal(
            str(risk_values["max_single_symbol_weight"])
        ),
        max_semiconductor_cluster_weight=Decimal(
            str(risk_values["max_semiconductor_cluster_weight"])
        ),
        max_leveraged_etf_weight=Decimal(
            str(risk_values["max_leveraged_etf_weight"])
        ),
        max_combined_leveraged_etf_weight=Decimal(
            str(risk_values["max_combined_leveraged_etf_weight"])
        ),
        max_gross_exposure=Decimal(str(portfolio_values["max_gross_exposure"])),
        soft_daily_loss_fraction=Decimal(
            str(risk_values["soft_daily_loss_fraction"])
        ),
        hard_daily_loss_fraction=Decimal(
            str(risk_values["hard_daily_loss_fraction"])
        ),
        block_entries_drawdown_fraction=Decimal(
            str(risk_values["block_entries_drawdown_fraction"])
        ),
        force_reduce_drawdown_fraction=Decimal(
            str(risk_values["force_reduce_drawdown_fraction"])
        ),
        force_reduce_core_fraction=Decimal(
            str(forward["loss_controls"]["force_reduce_core_fraction"])
        ),
        commission_rate=Decimal(str(commission["us_equity_rate"])),
        commission_waiver_threshold_usd=Decimal(
            str(commission["waive_if_order_total_usd_lte"])
        ),
        delay_penalty_bps=Decimal(str(execution["delay_penalty_bps"])),
        quantity_precision=Decimal(
            str(forward["execution"]["quantity_precision"])
        ),
        sell_only_symbols=frozenset(
            str(item) for item in universe["sell_only_symbols"]
        ),
        entry_symbols=frozenset(str(item) for item in universe["entry_symbols"]),
        semiconductor_symbols=frozenset(
            str(item) for item in clusters["SEMICONDUCTOR"]
        ),
        leveraged_symbols=frozenset(
            str(item) for item in universe["leveraged_symbols"]
        ),
        core_cap_exempt_arms=frozenset(
            str(item)
            for item in baseline["benchmark_core_cap_exempt_arms"]
        ),
    )


def _fill_contract(row: FillRow) -> Fill:
    return Fill.model_validate(row.payload_json)


def _attempt_payload(
    pending: PendingOrder,
    *,
    status: str,
    quote_id: str | None,
    fill: Fill | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "order_intent_id": pending.intent.order_intent_id,
        "arm_id": pending.intent.arm_id,
        "symbol": pending.intent.symbol,
        "side": pending.intent.side.value,
        "status": status,
        "quote_id": quote_id,
        "remaining_quantity_before": format(pending.remaining_quantity, "f"),
        "fill_quantity": "0",
        "fill_notional_usd": "0",
        "commission_usd": "0",
    }
    if detail is not None:
        payload["detail"] = detail
    if fill is not None:
        payload["fill_id"] = fill.fill_id
        payload["fill_quantity"] = format(fill.quantity, "f")
        payload["fill_notional_usd"] = format(fill.quantity * fill.price, "f")
        payload["commission_usd"] = format(fill.commission_usd, "f")
    return payload


def _regular_open(value: datetime) -> datetime:
    local = _aware(value).astimezone(NEW_YORK)
    return datetime.combine(local.date(), time(9, 30), tzinfo=NEW_YORK).astimezone(
        UTC
    )


def _session_day_start(value: datetime) -> datetime:
    local = _aware(value).astimezone(NEW_YORK)
    return datetime.combine(local.date(), time.min, tzinfo=NEW_YORK).astimezone(
        UTC
    )


def _regular_close(value: datetime) -> datetime:
    local = _aware(value).astimezone(NEW_YORK)
    return datetime.combine(local.date(), time(16, 0), tzinfo=NEW_YORK).astimezone(
        UTC
    )


def _regular_session_time(value: datetime) -> bool:
    local = _aware(value).astimezone(NEW_YORK)
    return (
        local.weekday() < 5
        and time(9, 30) <= local.time().replace(tzinfo=None) < time(16, 0)
    )


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _aware(value).isoformat().replace("+00:00", "Z")
