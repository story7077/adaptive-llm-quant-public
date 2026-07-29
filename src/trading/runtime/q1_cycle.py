from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from typing import Any, cast

from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trading.data.alpaca import FEED, PROVIDER
from trading.data.market_repository import MarketDataRepository
from trading.data.q1_pit import (
    AlignedDailyInputs,
    Q1PointInTimeDataError,
    Q1PointInTimeMarketData,
)
from trading.domain.contracts import NewsEvent, model_payload
from trading.domain.enums import OrderSide
from trading.domain.hashing import canonical_data, canonical_hash, stable_id
from trading.domain.paper import PaperAccountSpec
from trading.domain.q1 import (
    CashSettlementEvent,
    OrderEventType,
    PointInTimeSourceReference,
    Q1ArmId,
    Q1DecisionInputManifest,
    RiskEpisode,
    RiskEpisodeEvent,
    RiskEpisodeEventType,
    RiskSeverity,
    RiskTarget,
    StrategyEvaluationAnchor,
)
from trading.domain.q1_runtime import Q1OrderIntent
from trading.domain.time import Clock, SystemClock, require_aware_utc
from trading.execution.order_state import (
    OrderEventProvenance,
    Q1OrderClass,
    append_order_event,
    pending_orders,
    soft_stop_buy_cancellations,
    supersede_normal_orders,
)
from trading.llm.q1_overlay import (
    Q1LlmOverlayDecision,
    Q1OverlayState,
    apply_reduce_only_overlay,
    validate_bounded_evidence,
)
from trading.persistence.models import (
    CashSettlementEventRow,
    MarketCalendarSessionRow,
    NavSnapshotRow,
    NewsEventRow,
    PaperCycleRow,
    PortfolioDecisionRow,
    RiskEpisodeEventRow,
    RunRow,
)
from trading.persistence.paper import load_paper_account_spec
from trading.persistence.q1 import (
    CashSettlementRepository,
    OrderEventRepository,
    RiskEpisodeRepository,
    StrategyEvaluationAnchorRepository,
)
from trading.persistence.q1_runtime import (
    Q1OrderBook,
    append_arm_state,
    append_nav_snapshot,
    append_order_intent,
    append_risk_approval,
    append_strategy_decision,
    complete_fenced_cycle,
    latest_arm_state,
    load_q1_order_book,
    require_cycle_fence,
)
from trading.quant.allocator import (
    AllocationResult,
    TurnoverResult,
    allocate_b0_vol,
    allocate_q1,
    apply_turnover_control,
    compute_current_weights,
)
from trading.quant.covariance import (
    Q1MathError,
    ewma_annualized_variance,
    portfolio_variance,
)
from trading.quant.signals import AdjustedCloseObservation, Q1Signal, compute_q1_signal
from trading.risk.reconciliation import (
    Q1ReconciliationResult,
    Q1ReconciliationService,
)
from trading.risk.state_machine import (
    RiskCheckInput,
    RiskEngineConfig,
    RiskEpisodeProvenance,
    RiskMetrics,
    RiskQuote,
    RiskTransition,
    current_episode_targets,
    evaluate_risk_check,
    plan_risk_transition,
    target_progress_events,
)
from trading.runtime.provenance import workspace_code_version
from trading.runtime.q1_config import (
    critical_reconciliation_conditions,
    llm_provider_timeout_seconds,
    maximum_llm_evidence_events,
    maximum_quote_age_seconds,
    maximum_quote_skew_seconds,
    order_planning_config,
    risk_engine_config,
    settlement_policy,
)
from trading.runtime.q1_evaluation_cycle import (
    Q1EvaluationCycleProcessor,
    Q1EvaluationDataNotReady,
)
from trading.runtime.q1_execution_cycle import (
    Q1ExecutionCycleProcessor,
)
from trading.runtime.q1_paper import Q1_MODEL_VERSION, Q1PaperRuntimeService
from trading.runtime.q1_planning import (
    DecisionQuote,
    PlannedOrders,
    build_portfolio_decision,
    plan_normal_orders,
    plan_target_quantity_sell_orders,
    risk_approval_id,
)
from trading.runtime.q1_scheduler import (
    Q1_CYCLE_KINDS,
    VersionedMarketSession,
    normal_order_valid_until,
    risk_increase_allowed,
)
from trading.runtime.q1_state import Q1ArmState
from trading.settlement.service import (
    BusinessCalendar,
    SettlementProvenance,
    record_opening_settled_cash,
    settle_due_receivables,
)

Q1_DAILY_DATASET_VERSION = "alpaca_iex_adjusted_all_v1"
STRATEGY_ARMS = (
    Q1ArmId.B0_CASH,
    Q1ArmId.B0_QQQ,
    Q1ArmId.B0_VOL,
    Q1ArmId.Q1_DET,
    Q1ArmId.Q1_LLM,
)
RISK_ARMS = (
    Q1ArmId.LIVE_MIRROR,
    Q1ArmId.Q1_DET,
    Q1ArmId.Q1_LLM,
)
LlmOverlayProvider = Callable[
    [dict[str, Any]],
    Q1LlmOverlayDecision | Mapping[str, object] | None,
]
Q1NewsRefresher = Callable[[PaperCycleRow, datetime], object]


class Q1CycleError(RuntimeError):
    pass


class Q1CycleNotReady(Q1CycleError):
    pass


def _strategic_risk_candidates(
    states: Mapping[Q1ArmId, Q1ArmState],
    *,
    opening_anchor: bool,
) -> dict[Q1ArmId, Q1ArmState]:
    """Exclude only uncommitted, cash-only Q1 opening states from risk checks."""

    candidates = dict(states)
    if not opening_anchor:
        return candidates
    for arm_id in (Q1ArmId.Q1_DET, Q1ArmId.Q1_LLM):
        state = candidates.get(arm_id)
        if state is None:
            continue
        if (
            state.sequence != 0
            or state.positions
            or state.unsettled_receivables
            or state.settled_cash_usd != state.initial_nav_usd
            or state.evaluation_anchor_id is None
        ):
            raise Q1CycleError(
                f"{arm_id.value} opening state is not pristine cash-only"
            )
        candidates.pop(arm_id)
    return candidates


@dataclass(frozen=True, slots=True)
class StrategicRiskGate:
    arm_id: Q1ArmId
    episode_id: str | None
    latest_event_sequence: int
    consecutive_valid_release_checks: int
    transition: RiskTransition

    @property
    def released(self) -> bool:
        return (
            self.transition.active_episode is None
            and any(
                event.event_type is RiskEpisodeEventType.RELEASE
                for event in self.transition.new_events
            )
        )


class Q1PaperCycleProcessor:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        runtime: Q1PaperRuntimeService,
        account_file: Path,
        workspace_root: Path,
        clock: Clock | None = None,
        llm_overlay_provider: LlmOverlayProvider | None = None,
        llm_news_refresher: Q1NewsRefresher | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._runtime = runtime
        self._account_file = account_file
        self._workspace_root = workspace_root
        self._clock = clock or SystemClock()
        self._market = MarketDataRepository(session_factory)
        self._pit = Q1PointInTimeMarketData(session_factory)
        self._reconciliation = Q1ReconciliationService(session_factory)
        self._llm_overlay_provider = llm_overlay_provider
        self._llm_news_refresher = llm_news_refresher
        self._last_overlay: Q1LlmOverlayDecision | None = None
        self._last_overlay_request: dict[str, Any] | None = None
        self._last_overlay_provider_audit: dict[str, object] | None = None

    def process(self, cycle: PaperCycleRow) -> dict[str, Any]:
        if cycle.cycle_kind not in Q1_CYCLE_KINDS:
            raise Q1CycleError(f"Unsupported q1 cycle {cycle.cycle_kind!r}")
        if cycle.lease_owner is None:
            raise Q1CycleError("Claimed Q1 cycle has no lease owner")
        if cycle.cycle_kind == "Q1_BOOTSTRAP":
            return self._process_bootstrap(cycle)
        if cycle.cycle_kind == "Q1_STRATEGIC":
            return self._process_strategic(cycle)
        if cycle.cycle_kind == "Q1_SETTLEMENT":
            return self._process_settlement(cycle)
        if cycle.cycle_kind == "Q1_NAV_RISK":
            return self._process_nav_risk(cycle)
        if cycle.cycle_kind == "Q1_LLM_REVIEW":
            return self._process_llm_review(cycle)
        if cycle.cycle_kind == "Q1_EXECUTION":
            return self._process_execution(cycle)
        if cycle.cycle_kind == "Q1_DAILY_RESULT":
            return self._process_daily_result(cycle)
        raise Q1CycleError(f"Q1 cycle handler is missing for {cycle.cycle_kind}")

    def _process_bootstrap(self, cycle: PaperCycleRow) -> dict[str, Any]:
        now = self._clock.now()
        calendar = self._calendar_for_cycle(cycle)
        with self._session_factory() as session:
            existing_states = {
                arm_id.value: state
                for arm_id in Q1ArmId
                if (
                    state := latest_arm_state(
                        session,
                        run_id=cycle.run_id,
                        arm_id=arm_id.value,
                    )
                )
                is not None
            }
        inherited_initialized = {
            arm_id
            for arm_id in (
                Q1ArmId.HOLD.value,
                Q1ArmId.LIVE_MIRROR.value,
            )
            if arm_id in existing_states
        }
        if (
            inherited_initialized
            and inherited_initialized
            != {Q1ArmId.HOLD.value, Q1ArmId.LIVE_MIRROR.value}
        ):
            raise Q1CycleError(
                "HOLD and LIVE-MIRROR must be initialized atomically"
            )
        initializing = not inherited_initialized
        account = (
            load_paper_account_spec(self._account_file)
            if initializing
            else None
        )
        states: dict[str, Q1ArmState]
        if account is not None:
            settled_cash = _tradable_usd_cash(account)
            positions = {
                item.symbol: item.quantity
                for item in account.positions
            }
            states = {
                arm_id.value: Q1ArmState(
                    arm_id=arm_id.value,
                    initial_nav_usd=Decimal("1"),
                    settled_cash_usd=settled_cash,
                    unsettled_receivables=(),
                    positions=dict(positions),
                    sequence=0,
                    evaluation_anchor_id=None,
                )
                for arm_id in (Q1ArmId.HOLD, Q1ArmId.LIVE_MIRROR)
            }
        else:
            states = existing_states
        position_symbols = tuple(
            sorted(
                {
                    symbol
                    for state in states.values()
                    for symbol, quantity in state.positions.items()
                    if quantity > 0
                }
            )
        )
        quotes = self._fresh_decision_quotes(
            symbols=position_symbols,
            as_of=now,
            observed_after=calendar.open_at,
        )
        prices = {
            symbol: quote.midpoint
            for symbol, quote in quotes.items()
        }
        navs = {
            arm_id: state.nav(prices)
            for arm_id, state in states.items()
        }
        if any(nav <= 0 for nav in navs.values()):
            raise Q1CycleError("Session-open paper NAV must be positive")
        if initializing:
            inherited_nav = navs[Q1ArmId.HOLD.value]
            states = {
                arm_id: replace(state, initial_nav_usd=inherited_nav)
                for arm_id, state in states.items()
            }
        quote_manifest_hash = canonical_hash(
            {
                symbol: {
                    "quote_id": quote.quote_id,
                    "available_at": quote.available_at,
                    "midpoint": quote.midpoint,
                }
                for symbol, quote in sorted(quotes.items())
            }
        )
        source_manifest_hash = canonical_hash(
            {
                "calendar_session_id": calendar.calendar_session_id,
                "initial_account": account,
                "input_state_sequences": {
                    arm_id: state.sequence
                    for arm_id, state in sorted(states.items())
                },
                "quote_manifest_hash": quote_manifest_hash,
            }
        )
        input_manifest = {
            "cycle_id": cycle.cycle_id,
            "scheduled_at": _aware(cycle.scheduled_at),
            "calendar_session_id": calendar.calendar_session_id,
            "source_manifest_hash": source_manifest_hash,
            "config_manifest_hash": self._runtime.config.manifest_hash,
            "real_order_routing": False,
        }

        def writer(session: Session) -> dict[str, Any]:
            created: list[str] = []
            policy = settlement_policy(self._runtime.config)
            settlement_provenance = self._settlement_provenance(
                cycle,
                source_manifest_hash,
            )
            cash_repository = CashSettlementRepository(session)
            nav_ids: list[str] = []
            for arm_id, state in sorted(states.items()):
                existing = latest_arm_state(
                    session,
                    run_id=cycle.run_id,
                    arm_id=arm_id,
                    lock=True,
                )
                if existing is None:
                    if not initializing:
                        raise Q1CycleError(
                            f"{arm_id} disappeared during session bootstrap"
                        )
                    append_arm_state(
                        session,
                        run_id=cycle.run_id,
                        state=state,
                        source_cycle_id=cycle.cycle_id,
                        created_at=now,
                        expected_previous_sequence=None,
                    )
                    cash_repository.append(
                        record_opening_settled_cash(
                            arm_id=Q1ArmId(arm_id),
                            amount_usd=state.settled_cash_usd,
                            effective_at=now,
                            created_at=now,
                            calendar_session_id=calendar.calendar_session_id,
                            policy=policy,
                            provenance=settlement_provenance,
                        )
                    )
                    created.append(arm_id)
                elif existing.sequence != state.sequence:
                    raise Q1CycleError(
                        f"{arm_id} changed during session bootstrap"
                    )
                nav = navs[arm_id]
                weights = _general_weights(state, prices)
                nav_row = append_nav_snapshot(
                    session,
                    run_id=cycle.run_id,
                    arm_id=arm_id,
                    source_cycle_id=cycle.cycle_id,
                    as_of=now,
                    nav_usd=nav,
                    payload={
                        "schema_version": "q1_nav_v1",
                        "calendar_session_id": (
                            calendar.calendar_session_id
                        ),
                        "session_open_baseline": True,
                        "settled_cash_usd": str(
                            state.settled_cash_usd
                        ),
                        "unsettled_receivables_usd": str(
                            state.unsettled_cash_usd
                        ),
                        "positions_market_value_usd": str(
                            nav - state.total_cash_usd
                        ),
                        "actual_weights": {
                            symbol: str(value)
                            for symbol, value in weights.items()
                        },
                        "risk_state": (
                            "NORMAL"
                            if arm_id in {
                                Q1ArmId.LIVE_MIRROR.value,
                                Q1ArmId.Q1_DET.value,
                                Q1ArmId.Q1_LLM.value,
                            }
                            else "NOT_APPLICABLE"
                        ),
                        "release_condition_valid": False,
                        "reconciliation_ok": True,
                        "reconciliation_status": "OK",
                        "real_order_routing": False,
                    },
                    quote_manifest_hash=quote_manifest_hash,
                    algorithm_version="q1_math_core_v1",
                    config_manifest_hash=(
                        self._runtime.config.manifest_hash
                    ),
                    code_version=workspace_code_version(
                        self._workspace_root
                    ),
                    model_version=Q1_MODEL_VERSION,
                    source_manifest_hash=source_manifest_hash,
                )
                nav_ids.append(nav_row.nav_snapshot_id)
            run = session.get(RunRow, cycle.run_id)
            if run is None:
                raise Q1CycleError("Q1 run disappeared during bootstrap")
            if run.status == "PENDING_BOOTSTRAP":
                run.status = "AWAITING_EVALUATION_ANCHOR"
            return {
                "status": (
                    "HOLD_LIVE_MIRROR_INITIALIZED"
                    if initializing
                    else "SESSION_OPEN_NAV_RECORDED"
                ),
                "initialized_arms": created,
                "nav_snapshot_ids": nav_ids,
                "session_open_nav_usd": {
                    arm_id: str(nav)
                    for arm_id, nav in sorted(navs.items())
                },
                "quote_ids": {
                    symbol: quote.quote_id
                    for symbol, quote in sorted(quotes.items())
                },
                "real_order_routing": False,
            }

        return self._commit_cycle(
            cycle,
            cutoff=now,
            input_manifest=input_manifest,
            writer=writer,
            now=now,
        )

    def _process_strategic(self, cycle: PaperCycleRow) -> dict[str, Any]:
        self._last_overlay = None
        self._last_overlay_request = None
        self._last_overlay_provider_audit = None
        scheduled_at = _aware(cycle.scheduled_at)
        if self._llm_news_refresher is not None:
            with suppress(Exception):
                self._llm_news_refresher(cycle, scheduled_at)
        created_at = self._clock.now()
        calendar = self._calendar_for_cycle(cycle)
        if created_at >= normal_order_valid_until(
            calendar,
            schedule=self._runtime.schedule,
        ):
            raise Q1CycleNotReady("Strategic cycle missed the normal order window")
        hold_state = self._read_state(cycle.run_id, Q1ArmId.HOLD)
        existing_anchor = self._anchor(cycle.run_id)
        anchor_quote_symbols: set[str] = (
            set(hold_state.positions)
            if existing_anchor is None
            else set()
        )
        quotes = self._strategic_quote_bundle(
            anchor_symbols=tuple(sorted(anchor_quote_symbols)),
            as_of=created_at,
        )
        common_t0_nav = (
            hold_state.nav(
                {
                    symbol: quote.midpoint
                    for symbol, quote in quotes.items()
                }
            )
            if existing_anchor is None
            else existing_anchor.initial_nav_usd
        )
        if common_t0_nav <= 0:
            raise Q1CycleError("Common T0 NAV must be positive")
        anchor = existing_anchor or self._build_anchor(
            run_id=cycle.run_id,
            calendar=calendar,
            common_t0_nav=common_t0_nav,
            quotes=quotes,
            created_at=created_at,
        )
        states = self._strategy_states(
            run_id=cycle.run_id,
            anchor=anchor,
        )
        data_blocked_arms: dict[Q1ArmId, str] = {}
        try:
            qqq_annualized_variance, b0_aligned = (
                self._compute_b0_vol_variance(
                    calendar=calendar,
                    scheduled_at=scheduled_at,
                )
            )
            b0_vol_allocation = allocate_b0_vol(
                qqq_annualized_variance,
                config=self._runtime.math_config,
                config_manifest_hash=self._runtime.config.manifest_hash,
            )
        except (Q1CycleNotReady, Q1PointInTimeDataError, Q1MathError):
            qqq_annualized_variance = None
            b0_aligned = None
            b0_vol_allocation = None
            data_blocked_arms[Q1ArmId.B0_VOL] = (
                "QQQ_VOLATILITY_DATA_UNAVAILABLE"
            )
        try:
            signal, aligned = self._compute_signal(
                calendar=calendar,
                scheduled_at=scheduled_at,
            )
            q1_allocation = allocate_q1(
                signal,
                config=self._runtime.math_config,
            )
        except (Q1CycleNotReady, Q1PointInTimeDataError, Q1MathError):
            signal = None
            aligned = None
            q1_allocation = None
            data_blocked_arms[Q1ArmId.Q1_DET] = (
                "Q1_SIGNAL_DATA_UNAVAILABLE"
            )
            data_blocked_arms[Q1ArmId.Q1_LLM] = (
                "Q1_SIGNAL_DATA_UNAVAILABLE"
            )
        strategy_quotes = {
            symbol: quotes[symbol]
            for symbol in self._runtime.math_config.risky_symbols
            if symbol in quotes
        }
        q1_input_manifest = self._decision_manifest(
            calendar=calendar,
            aligned=aligned,
            quotes=strategy_quotes,
        )
        (
            risk_states,
            risk_quotes,
            _preliminary_skipped_risk_arms,
        ) = self._strategic_risk_context(
            run_id=cycle.run_id,
            strategy_states=_strategic_risk_candidates(
                states,
                opening_anchor=existing_anchor is None,
            ),
            strategy_quotes=strategy_quotes,
            as_of=created_at,
            available_quotes=quotes,
        )
        preliminary_risk_gates = self._prepare_strategic_risk_gates(
            cycle=cycle,
            calendar=calendar,
            created_at=created_at,
            states=risk_states,
            quotes=risk_quotes,
            source_manifest_hash=q1_input_manifest.source_manifest_hash,
        )
        if (
            q1_allocation is None
            or signal is None
            or set(strategy_quotes)
            != set(self._runtime.math_config.risky_symbols)
        ):
            overlay = {}
            overlay_state = Q1OverlayState.NO_CHANGE
            overlay_error = "Q1_SIGNAL_DATA_UNAVAILABLE"
        elif Q1ArmId.Q1_LLM in preliminary_risk_gates:
            overlay = dict(q1_allocation.target_weights)
            overlay_state = Q1OverlayState.NO_CHANGE
            overlay_error = "DETERMINISTIC_RISK_PRECEDENCE"
        else:
            overlay, overlay_state, overlay_error = self._llm_overlay(
                run_id=cycle.run_id,
                scheduled_at=scheduled_at,
                signal=signal,
                base_allocation=q1_allocation,
                states=states,
                quotes=strategy_quotes,
                created_at=created_at,
                input_manifest=q1_input_manifest,
                calendar=calendar,
            )
        final_created_at = self._clock.now()
        if final_created_at >= normal_order_valid_until(
            calendar,
            schedule=self._runtime.schedule,
        ):
            raise Q1CycleNotReady("Strategic cycle missed the normal order window")
        created_at = final_created_at
        quotes = self._strategic_quote_bundle(
            anchor_symbols=(
                tuple(sorted(anchor_quote_symbols))
                if existing_anchor is None
                else ()
            ),
            as_of=created_at,
        )
        common_t0_nav = (
            hold_state.nav(
                {
                    symbol: quote.midpoint
                    for symbol, quote in quotes.items()
                }
            )
            if existing_anchor is None
            else existing_anchor.initial_nav_usd
        )
        if common_t0_nav <= 0:
            raise Q1CycleError("Common T0 NAV must be positive")
        anchor = existing_anchor or self._build_anchor(
            run_id=cycle.run_id,
            calendar=calendar,
            common_t0_nav=common_t0_nav,
            quotes=quotes,
            created_at=created_at,
        )
        states = self._strategy_states(
            run_id=cycle.run_id,
            anchor=anchor,
        )
        strategy_quotes = {
            symbol: quotes[symbol]
            for symbol in self._runtime.math_config.risky_symbols
            if symbol in quotes
        }
        if "SOXX" not in strategy_quotes:
            data_blocked_arms[Q1ArmId.Q1_DET] = (
                "SOXX_EXECUTION_QUOTE_UNAVAILABLE"
            )
            data_blocked_arms[Q1ArmId.Q1_LLM] = (
                "SOXX_EXECUTION_QUOTE_UNAVAILABLE"
            )
        q1_input_manifest = self._decision_manifest(
            calendar=calendar,
            aligned=aligned,
            quotes=strategy_quotes,
        )
        b0_vol_input_manifest = self._decision_manifest(
            calendar=calendar,
            aligned=b0_aligned,
            quotes={"QQQ": strategy_quotes["QQQ"]},
        )
        b0_benchmark_input_manifest = self._decision_manifest(
            calendar=calendar,
            aligned=None,
            quotes={"QQQ": strategy_quotes["QQQ"]},
        )
        arm_input_manifests = {
            Q1ArmId.B0_CASH: b0_benchmark_input_manifest,
            Q1ArmId.B0_QQQ: b0_benchmark_input_manifest,
            Q1ArmId.B0_VOL: b0_vol_input_manifest,
            Q1ArmId.Q1_DET: q1_input_manifest,
            Q1ArmId.Q1_LLM: self._llm_augmented_manifest(
                q1_input_manifest
            ),
        }
        (
            risk_states,
            risk_quotes,
            skipped_risk_arms,
        ) = self._strategic_risk_context(
            run_id=cycle.run_id,
            strategy_states=_strategic_risk_candidates(
                states,
                opening_anchor=existing_anchor is None,
            ),
            strategy_quotes=strategy_quotes,
            as_of=created_at,
            available_quotes=quotes,
        )
        strategic_risk_gates = self._prepare_strategic_risk_gates(
            cycle=cycle,
            calendar=calendar,
            created_at=created_at,
            states=risk_states,
            quotes=risk_quotes,
            source_manifest_hash=q1_input_manifest.source_manifest_hash,
        )
        if (
            q1_allocation is None
            or signal is None
            or Q1ArmId.Q1_LLM in data_blocked_arms
        ):
            overlay = {}
            overlay_state = Q1OverlayState.NO_CHANGE
            overlay_error = "Q1_SIGNAL_DATA_UNAVAILABLE"
        elif Q1ArmId.Q1_LLM in strategic_risk_gates:
            overlay = dict(q1_allocation.target_weights)
            overlay_state = Q1OverlayState.NO_CHANGE
            overlay_error = "DETERMINISTIC_RISK_PRECEDENCE"
        elif self._last_overlay is not None:
            if not risk_increase_allowed(
                created_at,
                calendar,
                schedule=self._runtime.schedule,
            ):
                self._last_overlay = None
                overlay = dict(q1_allocation.target_weights)
                overlay_state = Q1OverlayState.NO_CHANGE
                overlay_error = "NEW_POLICY_CUTOFF_REACHED"
            else:
                overlay, overlay_state = self._apply_llm_overlay(
                    base_allocation=q1_allocation,
                    state=states[Q1ArmId.Q1_LLM],
                    midpoint_quotes={
                        symbol: quote.midpoint
                        for symbol, quote in strategy_quotes.items()
                    },
                    decision=self._last_overlay,
                    as_of=created_at,
                )
        prepared: dict[
            Q1ArmId,
            tuple[Any, TurnoverResult, PlannedOrders] | None,
        ] = {}
        for arm_id in STRATEGY_ARMS:
            if arm_id in strategic_risk_gates:
                prepared[arm_id] = None
                continue
            if arm_id in data_blocked_arms:
                prepared[arm_id] = None
                continue
            state = states[arm_id]
            current = compute_current_weights(
                positions=state.positions,
                settled_cash_usd=state.settled_cash_usd,
                unsettled_receivables={
                    item.receivable_id: item.amount_usd
                    for item in state.unsettled_receivables
                },
                midpoint_quotes={
                    symbol: quote.midpoint
                    for symbol, quote in strategy_quotes.items()
                },
                config=self._runtime.math_config,
            )
            current_weights = dict(current.weights)
            allocation, deterministic_target = self._allocation_for_arm(
                arm_id,
                q1=q1_allocation,
                b0_vol=b0_vol_allocation,
            )
            proposed_target = (
                overlay
                if arm_id is Q1ArmId.Q1_LLM
                else deterministic_target
            )
            turnover = apply_turnover_control(
                current_weights=current_weights,
                proposed_target_weights=proposed_target,
                current_nav_usd=current.nav_usd,
                used_normal_turnover=Decimal("0"),
                emergency_reduction=False,
                config=self._runtime.math_config,
                bypass_normal_turnover_cap=(
                    arm_id is Q1ArmId.B0_QQQ
                ),
            )
            final_target = dict(turnover.executable_target_weights)
            expected_volatility = self._expected_arm_volatility(
                arm_id=arm_id,
                target_weights=final_target,
                qqq_annualized_variance=qqq_annualized_variance,
                signal=signal,
            )
            arm_input_manifest = arm_input_manifests[arm_id]
            decision = build_portfolio_decision(
                run_id=cycle.run_id,
                arm_id=arm_id,
                source_cycle_id=cycle.cycle_id,
                input_state_sequence=state.sequence,
                decision_kind=turnover.decision_kind,
                scheduled_at=scheduled_at,
                signal_data_cutoff=scheduled_at,
                portfolio_state_as_of=created_at,
                quote_as_of=max(
                    quote.available_at
                    for quote in strategy_quotes.values()
                ),
                decision_created_at=created_at,
                valid_until=normal_order_valid_until(
                    calendar,
                    schedule=self._runtime.schedule,
                ),
                current_weights=current_weights,
                deterministic_target_weights=deterministic_target,
                final_target_weights=final_target,
                expected_annualized_volatility=expected_volatility,
                expected_one_way_turnover=(
                    turnover.executable_one_way_turnover
                ),
                used_daily_turnover_before=Decimal("0"),
                signal_hash=(
                    signal.signal_hash
                    if (
                        signal is not None
                        and arm_id
                        in {Q1ArmId.Q1_DET, Q1ArmId.Q1_LLM}
                    )
                    else None
                ),
                allocation_hash=(
                    None if allocation is None else allocation.allocation_hash
                ),
                llm_overlay_state=(
                    overlay_state.value
                    if arm_id is Q1ArmId.Q1_LLM
                    else "NOT_APPLICABLE"
                ),
                llm_policy_id=(
                    None
                    if arm_id is not Q1ArmId.Q1_LLM
                    else (
                        None
                        if self._last_overlay is None
                        else self._last_overlay.request_id
                    )
                ),
                diagnostics={
                    "signal": (
                        _signal_diagnostics(signal)
                        if (
                            signal is not None
                            and arm_id
                            in {Q1ArmId.Q1_DET, Q1ArmId.Q1_LLM}
                        )
                        else {}
                    ),
                    "b0_qqq_annualized_variance": (
                        qqq_annualized_variance
                        if arm_id in {Q1ArmId.B0_QQQ, Q1ArmId.B0_VOL}
                        else None
                    ),
                    "allocation": (
                        {}
                        if allocation is None
                        else allocation.diagnostics
                    ),
                    "turnover": {
                        "proposed": turnover.proposed_one_way_turnover,
                        "remaining_capacity": (
                            turnover.remaining_daily_capacity
                        ),
                        "alpha": turnover.interpolation_alpha,
                        "omitted_orders": [
                            asdict(item)
                            for item in turnover.omitted_orders
                        ],
                    },
                    "llm_failure": overlay_error,
                    "llm_policy": (
                        {}
                        if (
                            arm_id is not Q1ArmId.Q1_LLM
                            or self._last_overlay is None
                        )
                        else model_payload(self._last_overlay)
                    ),
                    "llm_policy_effective_time": (
                        None
                        if (
                            arm_id is not Q1ArmId.Q1_LLM
                            or self._last_overlay is None
                        )
                        else self._last_overlay.effective_time
                    ),
                    "llm_policy_expiry_time": (
                        None
                        if (
                            arm_id is not Q1ArmId.Q1_LLM
                            or self._last_overlay is None
                        )
                        else self._last_overlay.expiry_time
                    ),
                    "llm_provider_audit": (
                        {}
                        if (
                            arm_id is not Q1ArmId.Q1_LLM
                            or self._last_overlay_provider_audit is None
                        )
                        else self._last_overlay_provider_audit
                    ),
                    "llm_request": (
                        {}
                        if (
                            arm_id is not Q1ArmId.Q1_LLM
                            or self._last_overlay_request is None
                        )
                        else {
                            "request_id": self._last_overlay_request[
                                "request_id"
                            ],
                            "context_manifest_hash": (
                                self._last_overlay_request[
                                    "context_manifest_hash"
                                ]
                            ),
                            "evidence_event_ids": (
                                self._last_overlay_request[
                                    "allowed_evidence_event_ids"
                                ]
                            ),
                        }
                    ),
                    "matched_deterministic_input_hash": (
                        signal.signal_hash
                        if (
                            signal is not None
                            and arm_id
                            in {Q1ArmId.Q1_DET, Q1ArmId.Q1_LLM}
                        )
                        else None
                    ),
                },
                input_manifest=arm_input_manifest,
                worker_fence_token=_lease_owner(cycle),
                cycle_attempt_count=cycle.attempt_count,
            )
            planned = plan_normal_orders(
                decision=decision,
                turnover=turnover,
                positions=state.positions,
                settled_cash_usd=state.settled_cash_usd,
                quotes=strategy_quotes,
                source_cycle_id=cycle.cycle_id,
                input_state_sequence=state.sequence,
                valid_until=decision.valid_until,
                config=order_planning_config(self._runtime.config),
            )
            prepared[arm_id] = (decision, turnover, planned)
        source_manifest_hash = canonical_hash(
            {
                arm_id.value: manifest.source_manifest_hash
                for arm_id, manifest in sorted(
                    arm_input_manifests.items(),
                    key=lambda item: item[0].value,
                )
            }
        )
        cycle_input = {
            "cycle_id": cycle.cycle_id,
            "scheduled_at": scheduled_at,
            "signal_data_cutoff": scheduled_at,
            "calendar_session_id": calendar.calendar_session_id,
            "decision_input_manifest_hashes": {
                arm_id.value: manifest.manifest_hash
                for arm_id, manifest in sorted(
                    arm_input_manifests.items(),
                    key=lambda item: item[0].value,
                )
            },
            "signal_hash": (
                None if signal is None else signal.signal_hash
            ),
            "data_blocked_arms": {
                arm_id.value: reason
                for arm_id, reason in sorted(
                    data_blocked_arms.items(),
                    key=lambda item: item[0].value,
                )
            },
            "strategic_risk_gates": {
                arm_id.value: {
                    "episode_id": gate.episode_id,
                    "latest_event_sequence": gate.latest_event_sequence,
                    "consecutive_valid_release_checks": (
                        gate.consecutive_valid_release_checks
                    ),
                    "effective_severity": (
                        gate.transition.effective_severity.value
                    ),
                    "released": gate.released,
                }
                for arm_id, gate in strategic_risk_gates.items()
            },
            "risk_checks_skipped_for_missing_quotes": [
                arm_id.value
                for arm_id in skipped_risk_arms
            ],
            "config_manifest_hash": self._runtime.config.manifest_hash,
            "real_order_routing": False,
        }

        def writer(session: Session) -> dict[str, Any]:
            anchor_repository = StrategyEvaluationAnchorRepository(session)
            if existing_anchor is None:
                anchor_repository.append(anchor)
                self._append_strategy_opening_states(
                    session,
                    cycle=cycle,
                    anchor=anchor,
                    created_at=created_at,
                    source_manifest_hash=source_manifest_hash,
                )
            decision_ids: list[str] = []
            intent_ids: list[str] = []
            event_repository = OrderEventRepository(session)
            event_provenance = self._order_event_provenance(
                cycle,
                source_manifest_hash,
            )
            risk_repository = RiskEpisodeRepository(session)
            risk_episode_event_ids: list[str] = []
            risk_canceled_order_event_ids: list[str] = []
            released_risk_arms: list[str] = []
            for arm_id in RISK_ARMS:
                if arm_id not in risk_states:
                    continue
                gate = strategic_risk_gates.get(arm_id)
                current_episode = risk_repository.active(
                    run_id=cycle.run_id,
                    arm_id=arm_id.value,
                )
                if gate is None:
                    if current_episode is not None:
                        raise Q1CycleError(
                            f"{arm_id.value} risk episode activated during "
                            "strategic preparation"
                        )
                    continue
                expected_risk_state = risk_states[arm_id]
                actual_risk_state = latest_arm_state(
                    session,
                    run_id=cycle.run_id,
                    arm_id=arm_id.value,
                    lock=True,
                )
                if (
                    actual_risk_state is None
                    or actual_risk_state.sequence
                    != expected_risk_state.sequence
                ):
                    raise Q1CycleError(
                        f"{arm_id.value} changed during risk-release "
                        "preparation"
                    )
                if (
                    gate.transition.new_episode is not None
                ):
                    if current_episode is not None:
                        raise Q1CycleError(
                            f"{arm_id.value} risk episode activated during "
                            "strategic preparation"
                        )
                    new_events = gate.transition.new_events
                    if not new_events:
                        raise Q1CycleError(
                            "New typed risk episode lacks ACTIVATE event"
                        )
                    risk_repository.append_episode(
                        gate.transition.new_episode,
                        new_events[0],
                    )
                    risk_episode_event_ids.append(
                        new_events[0].risk_episode_event_id
                    )
                    for event in new_events[1:]:
                        risk_repository.append_event(event)
                        risk_episode_event_ids.append(
                            event.risk_episode_event_id
                        )
                elif gate.episode_id is None:
                    if (
                        current_episode is not None
                        or gate.transition.new_events
                    ):
                        raise Q1CycleError(
                            f"{arm_id.value} effect-only risk gate changed "
                            "during strategic preparation"
                        )
                else:
                    if (
                        current_episode is None
                        or current_episode.episode.risk_episode_id
                        != gate.episode_id
                        or current_episode.latest_event.event_sequence
                        != gate.latest_event_sequence
                    ):
                        raise Q1CycleError(
                            f"{arm_id.value} risk episode changed during "
                            "strategic preparation"
                        )
                    for event in gate.transition.new_events:
                        risk_repository.append_event(event)
                        risk_episode_event_ids.append(
                            event.risk_episode_event_id
                        )
                if gate.released:
                    released_risk_arms.append(arm_id.value)
            for arm_id in STRATEGY_ARMS:
                expected_state = states[arm_id]
                actual_state = latest_arm_state(
                    session,
                    run_id=cycle.run_id,
                    arm_id=arm_id.value,
                    lock=True,
                )
                if actual_state is None or (
                    actual_state.sequence != expected_state.sequence
                ):
                    raise Q1CycleError(
                        f"{arm_id.value} changed during strategic preparation"
                    )
                old_book = load_q1_order_book(
                    session,
                    run_id=cycle.run_id,
                    arm_id=arm_id.value,
                )
                if arm_id in strategic_risk_gates:
                    for event in soft_stop_buy_cancellations(
                        orders=old_book.descriptors,
                        events=old_book.events,
                        occurred_at=created_at,
                        available_at=created_at,
                        provenance=event_provenance,
                        source_cycle_id=cycle.cycle_id,
                    ):
                        event_repository.append(event)
                        risk_canceled_order_event_ids.append(event.event_id)
                    continue
                if arm_id in data_blocked_arms:
                    continue
                arm_preparation = prepared[arm_id]
                if arm_preparation is None:
                    raise Q1CycleError(
                        f"{arm_id.value} strategic preparation is missing"
                    )
                decision, _turnover, planned = arm_preparation
                decision_event_provenance = self._order_event_provenance(
                    cycle,
                    decision.source_manifest_hash,
                )
                superseded = supersede_normal_orders(
                    orders=old_book.descriptors,
                    events=old_book.events,
                    replacement_orders=tuple(
                        _descriptor(intent)
                        for intent in planned.intents
                    ),
                    occurred_at=created_at,
                    available_at=created_at,
                    provenance=decision_event_provenance,
                    source_cycle_id=cycle.cycle_id,
                )
                for event in superseded:
                    event_repository.append(event)
                append_strategy_decision(session, decision=decision)
                append_risk_approval(
                    session,
                    risk_decision_id=risk_approval_id(decision),
                    decision=decision,
                )
                decision_ids.append(decision.portfolio_decision_id)
                for intent in planned.intents:
                    append_order_intent(session, intent)
                    session.flush()
                    descriptor = _descriptor(intent)
                    event_repository.append(
                        append_order_event(
                            order=descriptor,
                            existing_events=(),
                            event_type=OrderEventType.CREATED,
                            occurred_at=created_at,
                            available_at=created_at,
                            provenance=decision_event_provenance,
                            source_cycle_id=cycle.cycle_id,
                        )
                    )
                    intent_ids.append(intent.order_intent_id)
            run = session.get(RunRow, cycle.run_id)
            if run is None:
                raise Q1CycleError("Q1 run disappeared during strategic commit")
            run.status = "RUNNING"
            return {
                "status": "STRATEGIC_DECISIONS_COMMITTED",
                "evaluation_anchor_id": anchor.evaluation_anchor_id,
                "decision_ids": decision_ids,
                "order_intent_ids": intent_ids,
                "risk_gated_arms": [
                    arm_id.value
                    for arm_id in strategic_risk_gates
                ],
                "risk_checks_skipped_for_missing_quotes": [
                    arm_id.value
                    for arm_id in skipped_risk_arms
                ],
                "released_risk_arms": released_risk_arms,
                "risk_episode_event_ids": risk_episode_event_ids,
                "risk_canceled_order_event_ids": (
                    risk_canceled_order_event_ids
                ),
                "signal_hash": (
                    None if signal is None else signal.signal_hash
                ),
                "data_blocked_arms": {
                    arm_id.value: reason
                    for arm_id, reason in sorted(
                        data_blocked_arms.items(),
                        key=lambda item: item[0].value,
                    )
                },
                "q1_det_minus_b0_vol": "PENDING_DAILY_EVALUATION",
                "q1_llm_minus_q1_det": "PENDING_DAILY_EVALUATION",
                "llm_overlay_state": overlay_state.value,
                "real_order_routing": False,
            }

        return self._commit_cycle(
            cycle,
            cutoff=scheduled_at,
            input_manifest=cycle_input,
            writer=writer,
            now=created_at,
        )

    def _commit_cycle(
        self,
        cycle: PaperCycleRow,
        *,
        cutoff: datetime,
        input_manifest: dict[str, Any],
        writer: Callable[[Session], dict[str, Any]],
        now: datetime,
    ) -> dict[str, Any]:
        with self._session_factory.begin() as session:
            locked = require_cycle_fence(
                session,
                cycle_id=cycle.cycle_id,
                lease_owner=_lease_owner(cycle),
                attempt_count=cycle.attempt_count,
                fallback_now=now,
            )
            output = writer(session)
            complete_fenced_cycle(
                locked,
                cutoff=cutoff,
                input_manifest=input_manifest,
                output_manifest=output,
                completed_at=now,
            )
            return output

    def _calendar_for_cycle(
        self,
        cycle: PaperCycleRow,
    ) -> VersionedMarketSession:
        session_date = _aware(cycle.scheduled_at).astimezone(
            self._runtime.schedule_timezone
        ).date()
        result = self._runtime.calendar_session(
            session_date=session_date,
            cutoff=_aware(cycle.scheduled_at),
        )
        if result is None:
            raise Q1CycleNotReady("Versioned market calendar is unavailable")
        return result

    def _fresh_decision_quotes(
        self,
        *,
        symbols: tuple[str, ...],
        as_of: datetime,
        observed_after: datetime | None = None,
        enforce_multi_symbol_skew: bool = True,
    ) -> dict[str, DecisionQuote]:
        if not symbols:
            return {}
        instant = require_aware_utc(as_of)
        rows: dict[str, DecisionQuote] = {}
        event_times: list[datetime] = []
        for symbol in symbols:
            row = self._market.latest_quote(
                provider=PROVIDER,
                feed=FEED,
                symbol=symbol,
                as_of=instant,
            )
            if row is None:
                raise Q1CycleNotReady(f"Fresh quote missing for {symbol}")
            event_time = _aware(row.event_time)
            available_at = _aware(row.available_at)
            if observed_after is not None and available_at <= observed_after:
                raise Q1CycleNotReady(
                    f"Quote for {symbol} does not follow session open"
                )
            age = (instant - event_time).total_seconds()
            if (
                age < 0
                or age > maximum_quote_age_seconds(self._runtime.config)
                or available_at > instant
                or row.bid_price <= 0
                or row.ask_price <= 0
                or row.ask_price < row.bid_price
            ):
                raise Q1CycleNotReady(f"Quote for {symbol} is stale or invalid")
            rows[symbol] = DecisionQuote(
                symbol=symbol,
                quote_id=row.quote_id,
                bid=row.bid_price,
                ask=row.ask_price,
                available_at=available_at,
            )
            event_times.append(event_time)
        if (
            enforce_multi_symbol_skew
            and event_times
            and (max(event_times) - min(event_times)).total_seconds()
            > maximum_quote_skew_seconds(self._runtime.config)
        ):
            raise Q1CycleNotReady("Decision quote bundle exceeds maximum skew")
        return rows

    def _strategic_quote_bundle(
        self,
        *,
        anchor_symbols: tuple[str, ...],
        as_of: datetime,
    ) -> dict[str, DecisionQuote]:
        """Separate inherited anchor valuation from the active decision bundle.

        HOLD's inherited positions determine the common T0 NAV, but they are
        not members of the clean strategy arms' QQQ/SOXX decision bundle.
        Every inherited quote must still be fresh and executable. Only the
        active QQQ/SOXX bundle is subject to the configured cross-symbol skew
        fence, so an unrelated inherited holding cannot block all clean arms.
        """

        required = tuple(sorted({*anchor_symbols, "QQQ"}))
        quotes = self._fresh_decision_quotes(
            symbols=required,
            as_of=as_of,
            enforce_multi_symbol_skew=False,
        )
        try:
            active_bundle = self._fresh_decision_quotes(
                symbols=("QQQ", "SOXX"),
                as_of=as_of,
            )
        except Q1CycleNotReady:
            quotes.pop("SOXX", None)
            return quotes
        quotes.update(active_bundle)
        return quotes

    def _strategic_risk_context(
        self,
        *,
        run_id: str,
        strategy_states: Mapping[Q1ArmId, Q1ArmState],
        strategy_quotes: Mapping[str, DecisionQuote],
        as_of: datetime,
        available_quotes: Mapping[str, DecisionQuote] | None = None,
    ) -> tuple[
        dict[Q1ArmId, Q1ArmState],
        dict[str, DecisionQuote],
        tuple[Q1ArmId, ...],
    ]:
        """Keep LIVE-MIRROR quote failures isolated from clean strategy arms."""

        states = dict(strategy_states)
        quotes = dict(strategy_quotes)
        skipped: list[Q1ArmId] = []
        for arm_id in (Q1ArmId.Q1_DET, Q1ArmId.Q1_LLM):
            state = states.get(arm_id)
            if state is None:
                continue
            missing_held = {
                symbol
                for symbol, quantity in state.positions.items()
                if quantity > 0 and symbol not in quotes
            }
            if missing_held:
                states.pop(arm_id)
                skipped.append(arm_id)
        if available_quotes is not None:
            quotes.update(available_quotes)
        live_state = self._read_state(run_id, Q1ArmId.LIVE_MIRROR)
        held_symbols = tuple(
            sorted(
                symbol
                for symbol, quantity in live_state.positions.items()
                if quantity > 0
            )
        )
        missing = tuple(
            symbol for symbol in held_symbols if symbol not in quotes
        )
        if missing:
            try:
                quotes.update(
                    self._fresh_decision_quotes(
                        symbols=missing,
                        as_of=as_of,
                    )
                )
            except Q1CycleNotReady:
                return states, dict(strategy_quotes), tuple(
                    (*skipped, Q1ArmId.LIVE_MIRROR)
                )
        live_quote_times = [
            quotes[symbol].available_at
            for symbol in held_symbols
        ]
        if (
            live_quote_times
            and (
                max(live_quote_times) - min(live_quote_times)
            ).total_seconds()
            > maximum_quote_skew_seconds(self._runtime.config)
        ):
            return states, dict(strategy_quotes), tuple(
                (*skipped, Q1ArmId.LIVE_MIRROR)
            )
        states[Q1ArmId.LIVE_MIRROR] = live_state
        return states, quotes, tuple(skipped)

    def _compute_signal(
        self,
        *,
        calendar: VersionedMarketSession,
        scheduled_at: datetime,
    ) -> tuple[Q1Signal, AlignedDailyInputs]:
        required = self._runtime.math_config.signal.minimum_completed_sessions
        previous = list(
            self._completed_calendar_sessions(
                calendar=calendar,
                cutoff=scheduled_at,
                required=required,
            )
        )
        if len(previous) < required:
            raise Q1CycleNotReady(
                "Versioned calendar lacks completed signal history"
            )
        expected_latest = previous[-1]
        aligned = self._pit.aligned_completed_daily_inputs(
            symbols=self._runtime.math_config.risky_symbols,
            current_session_date=calendar.session_date,
            expected_latest_completed_session=expected_latest.session_date,
            signal_data_cutoff=scheduled_at,
            minimum_completed_sessions=required,
            query_limit=required,
            dataset_version=Q1_DAILY_DATASET_VERSION,
        )
        sessions_by_date = {
            row.session_date: row
            for row in previous
        }
        observations: list[AdjustedCloseObservation] = []
        for symbol in self._runtime.math_config.risky_symbols:
            series = aligned.series[symbol]
            for index, session_date in enumerate(series.session_dates):
                row = sessions_by_date[session_date]
                observations.append(
                    AdjustedCloseObservation(
                        bar_id=series.bar_ids[index],
                        symbol=symbol,
                        session_id=row.calendar_session_id,
                        session_close_at=_aware(row.close_at),
                        adjusted_close=Decimal(
                            str(series.adjusted_closes[index])
                        ),
                        available_at=series.available_ats[index],
                    )
                )
        signal = compute_q1_signal(
            observations,
            completed_session_ids=tuple(
                row.calendar_session_id
                for row in previous
            ),
            calendar_session_id=calendar.calendar_session_id,
            expected_latest_completed_session_id=(
                expected_latest.calendar_session_id
            ),
            current_session_open_at=calendar.open_at,
            scheduled_at=scheduled_at,
            signal_data_cutoff=scheduled_at,
            config=self._runtime.math_config,
            config_manifest_hash=self._runtime.config.manifest_hash,
        )
        return signal, aligned

    def _compute_b0_vol_variance(
        self,
        *,
        calendar: VersionedMarketSession,
        scheduled_at: datetime,
    ) -> tuple[Decimal, AlignedDailyInputs]:
        """Estimate B0-VOL from QQQ alone, independent of SOXX availability."""

        required = self._runtime.math_config.signal.minimum_completed_sessions
        previous = self._completed_calendar_sessions(
            calendar=calendar,
            cutoff=scheduled_at,
            required=required,
        )
        if len(previous) < required:
            raise Q1CycleNotReady(
                "Versioned calendar lacks completed B0-VOL history"
            )
        expected_latest = previous[-1]
        aligned = self._pit.aligned_completed_daily_inputs(
            symbols=("QQQ",),
            current_session_date=calendar.session_date,
            expected_latest_completed_session=expected_latest.session_date,
            signal_data_cutoff=scheduled_at,
            minimum_completed_sessions=required,
            query_limit=required,
            dataset_version=Q1_DAILY_DATASET_VERSION,
        )
        closes = aligned.series["QQQ"].adjusted_closes
        with localcontext() as context:
            context.prec = self._runtime.math_config.covariance.decimal_precision
            returns = tuple(
                (closes[index] / closes[index - 1]).ln().quantize(
                    self._runtime.math_config.signal.return_quantum,
                    rounding=ROUND_HALF_EVEN,
                )
                for index in range(1, len(closes))
            )
            variance = ewma_annualized_variance(
                returns,
                parameters=self._runtime.math_config.covariance,
            )
        return variance, aligned

    def _completed_calendar_sessions(
        self,
        *,
        calendar: VersionedMarketSession,
        cutoff: datetime,
        required: int,
    ) -> tuple[MarketCalendarSessionRow, ...]:
        """Select the latest PIT calendar revision for each completed date."""

        with self._session_factory() as session:
            rows = tuple(
                session.scalars(
                    select(MarketCalendarSessionRow)
                    .where(
                        MarketCalendarSessionRow.calendar_version
                        == calendar.calendar_version,
                        MarketCalendarSessionRow.session_date
                        < calendar.session_date,
                        MarketCalendarSessionRow.available_at <= cutoff,
                    )
                    .order_by(
                        MarketCalendarSessionRow.session_date,
                        MarketCalendarSessionRow.available_at,
                        MarketCalendarSessionRow.calendar_session_id,
                    )
                )
            )
        latest_by_date = {
            row.session_date: row
            for row in rows
        }
        return tuple(
            latest_by_date[session_date]
            for session_date in sorted(latest_by_date)[-required:]
        )

    def _decision_manifest(
        self,
        *,
        calendar: VersionedMarketSession,
        aligned: AlignedDailyInputs | None,
        quotes: Mapping[str, DecisionQuote],
    ) -> Q1DecisionInputManifest:
        bars = tuple(
            sorted(
                (
                    PointInTimeSourceReference(
                        record_id=bar_id,
                        available_at=available_at,
                    )
                    for series in (
                        ()
                        if aligned is None
                        else aligned.series.values()
                    )
                    for bar_id, available_at in zip(
                        series.bar_ids,
                        series.available_ats,
                        strict=True,
                    )
                ),
                key=lambda item: item.record_id,
            )
        )
        quote_refs = tuple(
            PointInTimeSourceReference(
                record_id=quote.quote_id,
                available_at=quote.available_at,
            )
            for _, quote in sorted(quotes.items())
        )
        code_version = workspace_code_version(self._workspace_root)
        source_manifest_hash = canonical_hash(
            {
                "calendar_session_id": calendar.calendar_session_id,
                "bars": bars,
                "quotes": quote_refs,
                "daily_dataset_version": Q1_DAILY_DATASET_VERSION,
            }
        )
        return Q1DecisionInputManifest(
            calendar_session_id=calendar.calendar_session_id,
            source_bars=bars,
            quotes=quote_refs,
            config_manifest_hash=self._runtime.config.manifest_hash,
            code_version=code_version,
            model_version=Q1_MODEL_VERSION,
            source_manifest_hash=source_manifest_hash,
            manifest_hash=canonical_hash(
                {
                    "calendar_session_id": calendar.calendar_session_id,
                    "source_bars": bars,
                    "quotes": quote_refs,
                    "config_manifest_hash": (
                        self._runtime.config.manifest_hash
                    ),
                    "code_version": code_version,
                    "model_version": Q1_MODEL_VERSION,
                    "source_manifest_hash": source_manifest_hash,
                }
            ),
        )

    def _llm_augmented_manifest(
        self,
        base: Q1DecisionInputManifest,
    ) -> Q1DecisionInputManifest:
        """Bind the bounded commander request, response, and model audit."""

        if self._last_overlay_request is None:
            return base
        quote_refs = {
            item.record_id: item
            for item in base.quotes
        }
        raw_quotes = self._last_overlay_request.get("quotes")
        if isinstance(raw_quotes, Mapping):
            typed_quotes = cast(Mapping[object, object], raw_quotes)
            for raw_value in typed_quotes.values():
                raw = (
                    cast(Mapping[object, object], raw_value)
                    if isinstance(raw_value, Mapping)
                    else None
                )
                if raw is None:
                    continue
                quote_id = raw.get("quote_id")
                available_at = raw.get("available_at")
                if not isinstance(quote_id, str) or not isinstance(
                    available_at,
                    datetime,
                ):
                    continue
                quote_refs[quote_id] = PointInTimeSourceReference(
                    record_id=quote_id,
                    available_at=available_at,
                )
        audit = self._last_overlay_provider_audit or {}
        provider = audit.get("provider")
        model = audit.get("model")
        model_version = Q1_MODEL_VERSION
        if isinstance(provider, str) and isinstance(model, str):
            model_version = (
                f"{Q1_MODEL_VERSION}+commander:{provider}:{model}"
            )[:120]
        llm_provenance = {
            "request": self._last_overlay_request,
            "policy": (
                None
                if self._last_overlay is None
                else model_payload(self._last_overlay)
            ),
            "provider_audit": audit,
        }
        source_manifest_hash = canonical_hash(
            {
                "deterministic_source_manifest_hash": (
                    base.source_manifest_hash
                ),
                "llm_provenance": llm_provenance,
            }
        )
        ordered_quotes = tuple(
            quote_refs[quote_id]
            for quote_id in sorted(quote_refs)
        )
        content = {
            "calendar_session_id": base.calendar_session_id,
            "source_bars": base.source_bars,
            "quotes": ordered_quotes,
            "config_manifest_hash": base.config_manifest_hash,
            "code_version": base.code_version,
            "model_version": model_version,
            "source_manifest_hash": source_manifest_hash,
        }
        return Q1DecisionInputManifest(
            calendar_session_id=base.calendar_session_id,
            source_bars=base.source_bars,
            quotes=ordered_quotes,
            config_manifest_hash=base.config_manifest_hash,
            code_version=base.code_version,
            model_version=model_version,
            source_manifest_hash=source_manifest_hash,
            manifest_hash=canonical_hash(content),
        )

    def _allocation_for_arm(
        self,
        arm_id: Q1ArmId,
        *,
        q1: AllocationResult | None,
        b0_vol: AllocationResult | None,
    ) -> tuple[AllocationResult | None, dict[str, Decimal]]:
        if arm_id is Q1ArmId.B0_CASH:
            return None, {
                "QQQ": Decimal("0"),
                "SOXX": Decimal("0"),
                "USD_CASH": Decimal("1"),
            }
        if arm_id is Q1ArmId.B0_QQQ:
            return None, {
                "QQQ": Decimal("1"),
                "SOXX": Decimal("0"),
                "USD_CASH": Decimal("0"),
            }
        if arm_id is Q1ArmId.B0_VOL:
            if b0_vol is None:
                raise Q1CycleError("B0-VOL allocation is unavailable")
            return b0_vol, dict(b0_vol.target_weights)
        if arm_id in {Q1ArmId.Q1_DET, Q1ArmId.Q1_LLM}:
            if q1 is None:
                raise Q1CycleError("Q1 allocation is unavailable")
            return q1, dict(q1.target_weights)
        raise Q1CycleError(f"Unsupported strategic arm {arm_id}")

    def _expected_arm_volatility(
        self,
        *,
        arm_id: Q1ArmId,
        target_weights: Mapping[str, Decimal],
        qqq_annualized_variance: Decimal | None,
        signal: Q1Signal | None,
    ) -> Decimal:
        if arm_id is Q1ArmId.B0_CASH:
            return Decimal("0")
        if arm_id in {Q1ArmId.B0_QQQ, Q1ArmId.B0_VOL}:
            if qqq_annualized_variance is None:
                if arm_id is Q1ArmId.B0_QQQ:
                    return Decimal("0")
                raise Q1CycleError("QQQ volatility estimate is unavailable")
            return (
                target_weights.get("QQQ", Decimal("0"))
                * qqq_annualized_variance.sqrt()
            )
        if signal is None:
            raise Q1CycleError("Q1 signal is unavailable")
        return portfolio_variance(
            {
                symbol: target_weights[symbol]
                for symbol in self._runtime.math_config.risky_symbols
            },
            signal.covariance,
        ).sqrt()

    def _llm_overlay(
        self,
        *,
        run_id: str,
        scheduled_at: datetime,
        signal: Q1Signal,
        base_allocation: AllocationResult,
        states: Mapping[Q1ArmId, Q1ArmState],
        quotes: Mapping[str, DecisionQuote],
        created_at: datetime,
        input_manifest: Q1DecisionInputManifest,
        calendar: VersionedMarketSession,
    ) -> tuple[dict[str, Decimal], Q1OverlayState, str | None]:
        base = {
            symbol: float(value)
            for symbol, value in base_allocation.target_weights
        }
        state = states[Q1ArmId.Q1_LLM]
        qqq = base.get("QQQ", 0.0)
        soxx = base.get("SOXX", 0.0)
        current_portfolio = compute_current_weights(
            positions=state.positions,
            settled_cash_usd=state.settled_cash_usd,
            unsettled_receivables={
                item.receivable_id: item.amount_usd
                for item in state.unsettled_receivables
            },
            midpoint_quotes={
                symbol: quote.midpoint
                for symbol, quote in quotes.items()
            },
            config=self._runtime.math_config,
        )
        current = {
            symbol: float(value)
            for symbol, value in current_portfolio.weights
        }
        decision: Q1LlmOverlayDecision | None = None
        error: str | None = None
        news_events = self._bounded_llm_news_events(
            cutoff=scheduled_at,
            available_by=created_at,
        )
        evidence_ids = [
            event.news_event_id
            for event in news_events
        ]
        context = {
            "calendar_session_id": input_manifest.calendar_session_id,
            "scheduled_at": scheduled_at,
            "portfolio_state_as_of": created_at,
            "quote_as_of": max(
                quote.available_at
                for quote in quotes.values()
            ),
            "q1_det": {
                "signal_hash": signal.signal_hash,
                "allocation_hash": base_allocation.allocation_hash,
                "target_weights": base,
                "input_manifest_hash": input_manifest.manifest_hash,
            },
            "q1_llm": {
                "state_sequence": state.sequence,
                "positions": state.positions,
                "settled_cash_usd": state.settled_cash_usd,
                "unsettled_receivables_usd": state.unsettled_cash_usd,
                "current_nav_usd": current_portfolio.nav_usd,
                "current_weights": current,
            },
            "quotes": {
                symbol: {
                    "quote_id": quote.quote_id,
                    "bid": quote.bid,
                    "ask": quote.ask,
                    "available_at": quote.available_at,
                }
                for symbol, quote in sorted(quotes.items())
            },
            "news_events": [
                model_payload(event)
                for event in news_events
            ],
            "allowed_evidence_event_ids": evidence_ids,
            "allowed_outputs": {
                "risk_multiplier": [1.0, 0.75, 0.5],
                "block_new_entries": "boolean",
                "new_symbols": [],
                "order_quantities": "forbidden",
                "broker_actions": "forbidden",
            },
            "real_order_routing": False,
        }
        context_manifest_hash = canonical_hash(context)
        request_id = stable_id(
            "q1-llm-review-request",
            run_id,
            scheduled_at,
            context_manifest_hash,
        )
        request: dict[str, Any] = {
            "schema_version": "q1_llm_review_request_v1",
            "request_id": request_id,
            "context_manifest_hash": context_manifest_hash,
            **context,
        }
        self._last_overlay_request = request
        if self._llm_overlay_provider is not None:
            decision, error = self._invoke_llm_overlay_provider(request)
            provider_completed_at = self._clock.now()
            self._last_overlay_provider_audit = _provider_audit_payload(
                self._llm_overlay_provider,
                request_id,
            )
            if decision is not None:
                try:
                    if decision.request_id != request_id:
                        raise ValueError("Provider response request_id mismatch")
                    if (
                        decision.context_manifest_hash
                        != context_manifest_hash
                    ):
                        raise ValueError(
                            "Provider response context hash mismatch"
                        )
                    validate_bounded_evidence(
                        decision,
                        allowed_event_ids=set(evidence_ids),
                    )
                    if (
                        decision.created_at > provider_completed_at
                        or decision.effective_time > provider_completed_at
                    ):
                        raise ValueError(
                            "Provider policy cannot be future-effective"
                        )
                    if not risk_increase_allowed(
                        provider_completed_at,
                        calendar,
                        schedule=self._runtime.schedule,
                    ):
                        raise ValueError(
                            "Provider policy completed after the cutoff"
                        )
                except Exception:
                    decision = None
                    error = "INVALID_PROVIDER_OUTPUT"
        self._last_overlay = decision
        applied, overlay_state = self._apply_llm_overlay(
            base_allocation=base_allocation,
            state=state,
            midpoint_quotes={
                symbol: quote.midpoint
                for symbol, quote in quotes.items()
            },
            decision=decision,
            as_of=created_at,
        )
        result = applied
        if (
            result["QQQ"] > Decimal(str(qqq))
            or result["SOXX"] > Decimal(str(soxx))
        ):
            raise Q1CycleError("LLM overlay attempted to increase risk")
        return result, overlay_state, error

    def _apply_llm_overlay(
        self,
        *,
        base_allocation: AllocationResult,
        state: Q1ArmState,
        midpoint_quotes: Mapping[str, Decimal],
        decision: Q1LlmOverlayDecision | None,
        as_of: datetime,
    ) -> tuple[dict[str, Decimal], Q1OverlayState]:
        current_portfolio = compute_current_weights(
            positions=state.positions,
            settled_cash_usd=state.settled_cash_usd,
            unsettled_receivables={
                item.receivable_id: item.amount_usd
                for item in state.unsettled_receivables
            },
            midpoint_quotes=midpoint_quotes,
            config=self._runtime.math_config,
        )
        applied, overlay_state = apply_reduce_only_overlay(
            {
                symbol: float(value)
                for symbol, value in base_allocation.target_weights
            },
            current_weights={
                symbol: float(value)
                for symbol, value in current_portfolio.weights
            },
            decision=decision,
            as_of=as_of,
        )
        result = {
            symbol: Decimal(str(value))
            for symbol, value in applied.items()
        }
        deterministic = dict(base_allocation.target_weights)
        if any(
            result[symbol] > deterministic[symbol]
            for symbol in self._runtime.math_config.risky_symbols
        ):
            raise Q1CycleError("LLM overlay attempted to increase risk")
        return result, overlay_state

    def _bounded_llm_news_events(
        self,
        *,
        cutoff: datetime,
        available_by: datetime,
    ) -> tuple[NewsEvent, ...]:
        with self._session_factory() as session:
            rows = tuple(
                session.scalars(
                    select(NewsEventRow)
                    .where(NewsEventRow.as_of <= available_by)
                    .order_by(
                        NewsEventRow.as_of.desc(),
                        NewsEventRow.news_event_id,
                    )
                )
            )
        maximum = maximum_llm_evidence_events(self._runtime.config)
        valid: list[NewsEvent] = []
        for row in rows:
            try:
                event = NewsEvent.model_validate(row.payload_json)
            except Exception:
                continue
            if (
                event.output_hash != row.output_hash
                or event.as_of > available_by
                or event.data_available_cutoff > cutoff
                or event.created_at > available_by
                or event.expires_at <= available_by
            ):
                continue
            valid.append(event)
            if len(valid) == maximum:
                break
        return tuple(valid)

    def _invoke_llm_overlay_provider(
        self,
        request: dict[str, Any],
    ) -> tuple[Q1LlmOverlayDecision | None, str | None]:
        provider = self._llm_overlay_provider
        if provider is None:
            return None, "PROVIDER_UNAVAILABLE"
        result_queue: Queue[tuple[bool, object]] = Queue(maxsize=1)

        def invoke() -> None:
            try:
                result_queue.put((True, provider(request)))
            except Exception:
                result_queue.put((False, None))

        Thread(
            target=invoke,
            name="q1-10am-llm-provider",
            daemon=True,
        ).start()
        try:
            succeeded, raw = result_queue.get(
                timeout=float(
                    llm_provider_timeout_seconds(self._runtime.config)
                )
            )
        except Empty:
            return None, "PROVIDER_TIMEOUT"
        if not succeeded:
            return None, "PROVIDER_FAILURE"
        if raw is None:
            return None, "PROVIDER_NO_CHANGE"
        try:
            if isinstance(raw, Q1LlmOverlayDecision):
                return raw, None
            if isinstance(raw, Mapping):
                payload = cast(Mapping[str, object], raw)
                return (
                    Q1LlmOverlayDecision.model_validate(dict(payload)),
                    None,
                )
        except Exception:
            return None, "INVALID_PROVIDER_OUTPUT"
        return None, "INVALID_PROVIDER_OUTPUT"

    def _strategy_states(
        self,
        *,
        run_id: str,
        anchor: StrategyEvaluationAnchor,
    ) -> dict[Q1ArmId, Q1ArmState]:
        result: dict[Q1ArmId, Q1ArmState] = {}
        with self._session_factory() as session:
            for arm_id in STRATEGY_ARMS:
                state = latest_arm_state(
                    session,
                    run_id=run_id,
                    arm_id=arm_id.value,
                )
                if state is None:
                    state = Q1ArmState(
                        arm_id=arm_id.value,
                        initial_nav_usd=anchor.initial_nav_usd,
                        settled_cash_usd=anchor.initial_nav_usd,
                        unsettled_receivables=(),
                        positions={},
                        sequence=0,
                        evaluation_anchor_id=anchor.evaluation_anchor_id,
                    )
                if state.evaluation_anchor_id != anchor.evaluation_anchor_id:
                    raise Q1CycleError(
                        f"{arm_id.value} evaluation anchor mismatch"
                    )
                result[arm_id] = state
        return result

    def _append_strategy_opening_states(
        self,
        session: Session,
        *,
        cycle: PaperCycleRow,
        anchor: StrategyEvaluationAnchor,
        created_at: datetime,
        source_manifest_hash: str,
    ) -> None:
        policy = settlement_policy(self._runtime.config)
        provenance = self._settlement_provenance(
            cycle,
            source_manifest_hash,
        )
        repository = CashSettlementRepository(session)
        for arm_id in STRATEGY_ARMS:
            state = Q1ArmState(
                arm_id=arm_id.value,
                initial_nav_usd=anchor.initial_nav_usd,
                settled_cash_usd=anchor.initial_nav_usd,
                unsettled_receivables=(),
                positions={},
                sequence=0,
                evaluation_anchor_id=anchor.evaluation_anchor_id,
            )
            append_arm_state(
                session,
                run_id=cycle.run_id,
                state=state,
                source_cycle_id=cycle.cycle_id,
                created_at=created_at,
                expected_previous_sequence=None,
            )
            repository.append(
                record_opening_settled_cash(
                    arm_id=arm_id,
                    amount_usd=anchor.initial_nav_usd,
                    effective_at=created_at,
                    created_at=created_at,
                    calendar_session_id=anchor.calendar_session_id,
                    policy=policy,
                    provenance=provenance,
                )
            )
            append_nav_snapshot(
                session,
                run_id=cycle.run_id,
                arm_id=arm_id.value,
                source_cycle_id=cycle.cycle_id,
                as_of=created_at,
                nav_usd=anchor.initial_nav_usd,
                payload={
                    "schema_version": "q1_nav_v1",
                    "calendar_session_id": anchor.calendar_session_id,
                    "session_open_baseline": True,
                    "settled_cash_usd": str(anchor.initial_nav_usd),
                    "unsettled_receivables_usd": "0",
                    "positions_market_value_usd": "0",
                    "actual_weights": {
                        "USD_CASH": "1",
                    },
                    "risk_state": (
                        "NORMAL"
                        if arm_id in {
                            Q1ArmId.Q1_DET,
                            Q1ArmId.Q1_LLM,
                        }
                        else "NOT_APPLICABLE"
                    ),
                    "release_condition_valid": False,
                    "reconciliation_ok": True,
                    "reconciliation_status": "OK",
                    "real_order_routing": False,
                },
                quote_manifest_hash=anchor.quote_manifest_hash,
                algorithm_version="q1_math_core_v1",
                config_manifest_hash=self._runtime.config.manifest_hash,
                code_version=workspace_code_version(
                    self._workspace_root
                ),
                model_version=Q1_MODEL_VERSION,
                source_manifest_hash=source_manifest_hash,
            )

    def _build_anchor(
        self,
        *,
        run_id: str,
        calendar: VersionedMarketSession,
        common_t0_nav: Decimal,
        quotes: Mapping[str, DecisionQuote],
        created_at: datetime,
    ) -> StrategyEvaluationAnchor:
        quote_manifest_hash = canonical_hash(
            {
                symbol: {
                    "quote_id": quote.quote_id,
                    "available_at": quote.available_at,
                    "midpoint": quote.midpoint,
                }
                for symbol, quote in sorted(quotes.items())
            }
        )
        source_manifest_hash = canonical_hash(
            {
                "calendar_session_id": calendar.calendar_session_id,
                "quote_manifest_hash": quote_manifest_hash,
                "account_basis": "HOLD_INHERITED_ACCOUNT",
            }
        )
        code_version = workspace_code_version(self._workspace_root)
        content = {
            "run_id": run_id,
            "calendar_session_id": calendar.calendar_session_id,
            "common_t0_at": created_at,
            "initial_nav_usd": common_t0_nav,
            "quote_manifest_hash": quote_manifest_hash,
            "config_manifest_hash": self._runtime.config.manifest_hash,
            "code_version": code_version,
            "model_version": Q1_MODEL_VERSION,
            "source_manifest_hash": source_manifest_hash,
        }
        anchor_hash = canonical_hash(content)
        return StrategyEvaluationAnchor(
            evaluation_anchor_id=stable_id(
                "q1-evaluation-anchor",
                run_id,
                anchor_hash,
            ),
            run_id=run_id,
            calendar_session_id=calendar.calendar_session_id,
            common_t0_at=created_at,
            initial_nav_usd=common_t0_nav,
            quote_manifest_hash=quote_manifest_hash,
            config_manifest_hash=self._runtime.config.manifest_hash,
            code_version=code_version,
            model_version=Q1_MODEL_VERSION,
            source_manifest_hash=source_manifest_hash,
            anchor_hash=anchor_hash,
            created_at=created_at,
        )

    def _anchor(self, run_id: str) -> StrategyEvaluationAnchor | None:
        with self._session_factory() as session:
            row = StrategyEvaluationAnchorRepository(session).for_run(run_id)
            return (
                None
                if row is None
                else StrategyEvaluationAnchor.model_validate(row.payload_json)
            )

    def _read_state(self, run_id: str, arm_id: Q1ArmId) -> Q1ArmState:
        with self._session_factory() as session:
            state = latest_arm_state(
                session,
                run_id=run_id,
                arm_id=arm_id.value,
            )
        if state is None:
            raise Q1CycleNotReady(f"{arm_id.value} is not initialized")
        return state

    def _all_initialized_states(
        self,
        run_id: str,
    ) -> dict[str, Q1ArmState]:
        result: dict[str, Q1ArmState] = {}
        with self._session_factory() as session:
            for arm_id in Q1ArmId:
                state = latest_arm_state(
                    session,
                    run_id=run_id,
                    arm_id=arm_id.value,
                )
                if state is not None:
                    result[arm_id.value] = state
        if Q1ArmId.HOLD.value not in result:
            raise Q1CycleNotReady("HOLD is not initialized")
        if Q1ArmId.LIVE_MIRROR.value not in result:
            raise Q1CycleNotReady("LIVE-MIRROR is not initialized")
        return result

    def _risk_nav_baselines(
        self,
        *,
        run_id: str,
        arm_id: str,
        calendar_session_id: str,
        current_nav: Decimal,
    ) -> tuple[Decimal, Decimal]:
        with self._session_factory() as session:
            rows = tuple(
                session.scalars(
                    select(NavSnapshotRow)
                    .where(
                        NavSnapshotRow.run_id == run_id,
                        NavSnapshotRow.arm_id == arm_id,
                        NavSnapshotRow.algorithm_version
                        == "q1_math_core_v1",
                    )
                    .order_by(
                        NavSnapshotRow.as_of,
                        NavSnapshotRow.nav_snapshot_id,
                    )
                )
            )
        session_open_rows = tuple(
            row
            for row in rows
            if (
                row.payload_json.get("calendar_session_id")
                == calendar_session_id
                and row.payload_json.get("session_open_baseline") is True
            )
        )
        if not session_open_rows:
            raise Q1CycleNotReady(
                f"Session-open NAV baseline is missing for {arm_id}"
            )
        session_open = Decimal(session_open_rows[0].nav_usd)
        running_peak = max(
            (Decimal(row.nav_usd) for row in rows),
            default=current_nav,
        )
        return session_open, max(running_peak, current_nav)

    def _current_portfolio_annualized_volatility(
        self,
        run_id: str,
        arm_id: str,
        *,
        state: Q1ArmState,
        prices: Mapping[str, Decimal],
    ) -> Decimal | None:
        held_risk = {
            symbol: quantity
            for symbol, quantity in state.positions.items()
            if symbol in {"QQQ", "SOXX"} and quantity > 0
        }
        if not held_risk:
            return Decimal("0")
        with self._session_factory() as session:
            rows = tuple(
                session.scalars(
                    select(PortfolioDecisionRow)
                    .where(
                        PortfolioDecisionRow.run_id == run_id,
                        PortfolioDecisionRow.arm_id == arm_id,
                        PortfolioDecisionRow.algorithm_version
                        == "q1_math_core_v1",
                    )
                    .order_by(
                        PortfolioDecisionRow.decision_created_at.desc(),
                        PortfolioDecisionRow.portfolio_decision_id.desc(),
                    )
                )
            )
        for row in rows:
            diagnostics = row.payload_json.get("diagnostics")
            if not isinstance(diagnostics, dict):
                continue
            typed_diagnostics = cast(dict[str, object], diagnostics)
            signal = typed_diagnostics.get("signal")
            if not isinstance(signal, dict):
                continue
            covariance = cast(dict[str, object], signal).get("covariance")
            if not isinstance(covariance, dict):
                continue
            nav = state.nav(dict(prices))
            weights = {
                symbol: (
                    state.positions.get(symbol, Decimal("0"))
                    * prices[symbol]
                    / nav
                )
                for symbol in ("QQQ", "SOXX")
            }
            try:
                typed_covariance = cast(dict[str, object], covariance)
                qqq_row = cast(dict[str, object], typed_covariance["QQQ"])
                soxx_row = cast(dict[str, object], typed_covariance["SOXX"])
                variance = (
                    weights["QQQ"]
                    * weights["QQQ"]
                    * Decimal(str(qqq_row["QQQ"]))
                    + Decimal("2")
                    * weights["QQQ"]
                    * weights["SOXX"]
                    * Decimal(str(qqq_row["SOXX"]))
                    + weights["SOXX"]
                    * weights["SOXX"]
                    * Decimal(str(soxx_row["SOXX"]))
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise Q1CycleError(
                    "Stored Q1 covariance diagnostics are malformed"
                ) from exc
            if variance < 0:
                raise Q1CycleError(
                    "Stored Q1 covariance produced negative portfolio variance"
                )
            return variance.sqrt()
        return None

    def _active_risk_episode(
        self,
        *,
        run_id: str,
        arm_id: str,
    ) -> tuple[RiskEpisode | None, tuple[RiskEpisodeEvent, ...]]:
        with self._session_factory() as session:
            active = RiskEpisodeRepository(session).active(
                run_id=run_id,
                arm_id=arm_id,
            )
            if active is None:
                return None, ()
            event_rows = tuple(
                session.scalars(
                    select(RiskEpisodeEventRow)
                    .where(
                        RiskEpisodeEventRow.risk_episode_id
                        == active.episode.risk_episode_id
                    )
                    .order_by(
                        RiskEpisodeEventRow.event_sequence,
                        RiskEpisodeEventRow.risk_episode_event_id,
                    )
                )
            )
            return (
                RiskEpisode.model_validate(active.episode.payload_json),
                tuple(
                    RiskEpisodeEvent.model_validate(row.payload_json)
                    for row in event_rows
                ),
            )

    def _read_order_book(
        self,
        run_id: str,
        arm_id: str,
    ) -> Q1OrderBook:
        with self._session_factory() as session:
            return load_q1_order_book(
                session,
                run_id=run_id,
                arm_id=arm_id,
            )

    def _consecutive_valid_release_checks(
        self,
        *,
        run_id: str,
        arm_id: str,
        calendar_session_id: str,
        as_of: datetime,
    ) -> int:
        with self._session_factory() as session:
            rows = tuple(
                session.scalars(
                    select(NavSnapshotRow)
                    .where(
                        NavSnapshotRow.run_id == run_id,
                        NavSnapshotRow.arm_id == arm_id,
                        NavSnapshotRow.algorithm_version
                        == "q1_math_core_v1",
                        NavSnapshotRow.as_of <= as_of,
                    )
                    .order_by(
                        NavSnapshotRow.as_of.desc(),
                        NavSnapshotRow.nav_snapshot_id.desc(),
                    )
                )
            )
        count = 0
        for row in rows:
            payload = row.payload_json
            if payload.get("calendar_session_id") != calendar_session_id:
                continue
            if (
                payload.get("release_condition_valid") is not True
                or payload.get("reconciliation_ok", True) is not True
            ):
                break
            count += 1
        return count

    def _prepare_strategic_risk_gates(
        self,
        *,
        cycle: PaperCycleRow,
        calendar: VersionedMarketSession,
        created_at: datetime,
        states: Mapping[Q1ArmId, Q1ArmState],
        quotes: Mapping[str, DecisionQuote],
        source_manifest_hash: str,
    ) -> dict[Q1ArmId, StrategicRiskGate]:
        result: dict[Q1ArmId, StrategicRiskGate] = {}
        config = risk_engine_config(self._runtime.config)
        for arm_id in RISK_ARMS:
            active_episode, existing_events = self._active_risk_episode(
                run_id=cycle.run_id,
                arm_id=arm_id.value,
            )
            state = states.get(arm_id)
            if state is None:
                if active_episode is None:
                    continue
                raise Q1CycleNotReady(f"{arm_id.value} is not initialized")
            prices = {
                symbol: quote.midpoint
                for symbol, quote in quotes.items()
            }
            current_nav = state.nav(prices)
            session_open, running_peak = self._risk_nav_baselines(
                run_id=cycle.run_id,
                arm_id=arm_id.value,
                calendar_session_id=calendar.calendar_session_id,
                current_nav=current_nav,
            )
            reconciliation = self._reconcile_state(
                run_id=cycle.run_id,
                arm_id=arm_id.value,
                state=state,
                as_of=created_at,
            )
            reconciliation_status = _reconciliation_status(
                reconciliation
            )
            check = RiskCheckInput(
                arm_id=arm_id,
                calendar_session_id=calendar.calendar_session_id,
                scheduled_at=_aware(cycle.scheduled_at),
                decision_created_at=created_at,
                positions=state.positions,
                settled_cash_usd=state.settled_cash_usd,
                unsettled_receivables_usd=state.unsettled_cash_usd,
                quotes={
                    symbol: RiskQuote(
                        symbol=symbol,
                        quote_id=quotes[symbol].quote_id,
                        midpoint=quotes[symbol].midpoint,
                    )
                    for symbol, quantity in state.positions.items()
                    if quantity > 0
                },
                session_open_nav_usd=session_open,
                running_peak_nav_usd=running_peak,
                portfolio_annualized_vol=(
                    self._current_portfolio_annualized_volatility(
                        cycle.run_id,
                        arm_id.value,
                        state=state,
                        prices=prices,
                    )
                ),
                reconciliation_ok=reconciliation.ok,
                critical_reconciliation_condition=(
                    reconciliation.is_critical(
                        critical_reconciliation_conditions(
                            self._runtime.config
                        )
                    )
                ),
                reconciliation_status=reconciliation_status,
            )
            metrics = evaluate_risk_check(check, config)
            valid_checks = self._consecutive_valid_release_checks(
                run_id=cycle.run_id,
                arm_id=arm_id.value,
                calendar_session_id=calendar.calendar_session_id,
                as_of=created_at,
            )
            provenance = RiskEpisodeProvenance(
                run_id=cycle.run_id,
                config_manifest_hash=self._runtime.config.manifest_hash,
                code_version=workspace_code_version(self._workspace_root),
                model_version=Q1_MODEL_VERSION,
                source_manifest_hash=canonical_hash(
                    {
                        "decision_source_manifest_hash": (
                            source_manifest_hash
                        ),
                        "reconciliation_result_hash": (
                            reconciliation.result_hash
                        ),
                    }
                ),
                worker_fence_token=_lease_owner(cycle),
                cycle_attempt_count=cycle.attempt_count,
            )
            transition = plan_risk_transition(
                check=check,
                metrics=metrics,
                config=config,
                provenance=provenance,
                active_episode=active_episode,
                existing_episode_events=existing_events,
                is_next_session_strategic_cycle=True,
                consecutive_valid_release_checks=valid_checks,
                source_cycle_id=cycle.cycle_id,
            )
            if (
                active_episode is None
                and transition.new_episode is None
                and transition.effective_severity is RiskSeverity.NORMAL
            ):
                continue
            latest_sequence = max(
                (
                    event.event_sequence
                    for event in existing_events
                ),
                default=0,
            )
            result[arm_id] = StrategicRiskGate(
                arm_id=arm_id,
                episode_id=(
                    transition.new_episode.risk_episode_id
                    if transition.new_episode is not None
                    else (
                        None
                        if active_episode is None
                        else active_episode.risk_episode_id
                    )
                ),
                latest_event_sequence=latest_sequence,
                consecutive_valid_release_checks=valid_checks,
                transition=transition,
            )
        return result

    def _reconcile_state(
        self,
        *,
        run_id: str,
        arm_id: str,
        state: Q1ArmState,
        as_of: datetime,
    ) -> Q1ReconciliationResult:
        return self._reconciliation.check(
            run_id=run_id,
            arm_id=arm_id,
            state=state,
            as_of=as_of,
        )

    def _order_event_provenance(
        self,
        cycle: PaperCycleRow,
        source_manifest_hash: str,
    ) -> OrderEventProvenance:
        return OrderEventProvenance(
            config_manifest_hash=self._runtime.config.manifest_hash,
            code_version=workspace_code_version(self._workspace_root),
            model_version=Q1_MODEL_VERSION,
            source_manifest_hash=source_manifest_hash,
            worker_fence_token=_lease_owner(cycle),
            cycle_attempt_count=cycle.attempt_count,
        )

    def _settlement_provenance(
        self,
        cycle: PaperCycleRow,
        source_manifest_hash: str,
    ) -> SettlementProvenance:
        return SettlementProvenance(
            run_id=cycle.run_id,
            source_cycle_id=cycle.cycle_id,
            config_manifest_hash=self._runtime.config.manifest_hash,
            code_version=workspace_code_version(self._workspace_root),
            model_version=Q1_MODEL_VERSION,
            source_manifest_hash=source_manifest_hash,
            worker_fence_token=_lease_owner(cycle),
            cycle_attempt_count=cycle.attempt_count,
        )

    def _process_settlement(self, cycle: PaperCycleRow) -> dict[str, Any]:
        now = self._clock.now()
        calendar_session = self._calendar_for_cycle(cycle)
        with self._session_factory() as session:
            calendar_dates = tuple(
                session.scalars(
                    select(MarketCalendarSessionRow.session_date)
                    .where(
                        MarketCalendarSessionRow.calendar_version
                        == calendar_session.calendar_version,
                        MarketCalendarSessionRow.available_at <= now,
                    )
                    .order_by(MarketCalendarSessionRow.session_date)
                )
            )
        business_calendar = BusinessCalendar(
            version=calendar_session.calendar_version,
            sessions=calendar_dates,
        )
        source_manifest_hash = canonical_hash(
            {
                "calendar_session_id": calendar_session.calendar_session_id,
                "through_session": calendar_session.session_date,
                "settlement_policy": settlement_policy(
                    self._runtime.config
                ).version,
                "calendar_session_dates": calendar_dates,
            }
        )

        def writer(session: Session) -> dict[str, Any]:
            repository = CashSettlementRepository(session)
            policy = settlement_policy(self._runtime.config)
            provenance = self._settlement_provenance(
                cycle,
                source_manifest_hash,
            )
            appended: list[str] = []
            for arm_id in Q1ArmId:
                state = latest_arm_state(
                    session,
                    run_id=cycle.run_id,
                    arm_id=arm_id.value,
                    lock=True,
                )
                if state is None:
                    continue
                rows = tuple(
                    session.scalars(
                        select(CashSettlementEventRow)
                        .where(
                            CashSettlementEventRow.run_id == cycle.run_id,
                            CashSettlementEventRow.arm_id == arm_id.value,
                            CashSettlementEventRow.created_at <= now,
                        )
                        .order_by(
                            CashSettlementEventRow.effective_at,
                            CashSettlementEventRow.cash_settlement_event_id,
                        )
                    )
                )
                events = tuple(
                    CashSettlementEvent.model_validate(row.payload_json)
                    for row in rows
                )
                due = settle_due_receivables(
                    events=events,
                    through_session=calendar_session.session_date,
                    effective_at=max(now, calendar_session.open_at),
                    created_at=now,
                    calendar_session_id=calendar_session.calendar_session_id,
                    policy=policy,
                    calendar=business_calendar,
                    provenance=provenance,
                )
                current = state
                for event in due:
                    repository.append(event)
                    previous_sequence = current.sequence
                    if event.receivable_id is None:
                        raise Q1CycleError(
                            "Settlement event has no receivable ID"
                        )
                    current = current.settle(event.receivable_id)
                    append_arm_state(
                        session,
                        run_id=cycle.run_id,
                        state=current,
                        source_cycle_id=cycle.cycle_id,
                        created_at=now,
                        expected_previous_sequence=previous_sequence,
                    )
                    appended.append(event.cash_settlement_event_id)
            return {
                "status": (
                    "RECEIVABLES_SETTLED"
                    if appended
                    else "NO_DUE_RECEIVABLES"
                ),
                "cash_settlement_event_ids": appended,
                "real_order_routing": False,
            }

        return self._commit_cycle(
            cycle,
            cutoff=now,
            input_manifest={
                "cycle_id": cycle.cycle_id,
                "calendar_session_id": (
                    calendar_session.calendar_session_id
                ),
                "source_manifest_hash": source_manifest_hash,
                "config_manifest_hash": self._runtime.config.manifest_hash,
                "real_order_routing": False,
            },
            writer=writer,
            now=now,
        )

    @staticmethod
    def _risk_check_quotes(
        *,
        arm_id: str,
        state: Q1ArmState,
        fresh_quotes: Mapping[str, DecisionQuote],
        active_targets: Mapping[str, RiskTarget],
    ) -> dict[str, RiskQuote]:
        """Value achieved latched targets without requiring another quote."""

        result: dict[str, RiskQuote] = {}
        for symbol, quantity in state.positions.items():
            if quantity <= 0:
                continue
            fresh = fresh_quotes.get(symbol)
            if fresh is not None:
                result[symbol] = RiskQuote(
                    symbol=symbol,
                    quote_id=fresh.quote_id,
                    midpoint=fresh.midpoint,
                )
                continue
            target = active_targets.get(symbol)
            if (
                target is None
                or quantity > target.target_quantity
                or target.trigger_price is None
            ):
                raise Q1CycleNotReady(
                    f"Fresh risk quote missing for {arm_id}/{symbol}"
                )
            result[symbol] = RiskQuote(
                symbol=symbol,
                quote_id=target.trigger_quote_id,
                midpoint=target.trigger_price,
            )
        return result

    def _process_nav_risk(self, cycle: PaperCycleRow) -> dict[str, Any]:
        now = self._clock.now()
        calendar = self._calendar_for_cycle(cycle)
        states = self._all_initialized_states(cycle.run_id)
        all_held_symbols = tuple(
            sorted(
                {
                    symbol
                    for state in states.values()
                    for symbol, quantity in state.positions.items()
                    if quantity > 0
                }
            )
        )
        active_risk: dict[
            str,
            tuple[RiskEpisode | None, tuple[RiskEpisodeEvent, ...]],
        ] = {}
        active_targets: dict[str, dict[str, RiskTarget]] = {}
        risk_required_symbols: set[str] = set()
        for arm_id in RISK_ARMS:
            state = states.get(arm_id.value)
            if state is None:
                continue
            episode, events = self._active_risk_episode(
                run_id=cycle.run_id,
                arm_id=arm_id.value,
            )
            active_risk[arm_id.value] = (episode, events)
            targets = (
                ()
                if episode is None
                else current_episode_targets(episode, events)
            )
            target_map = {
                target.symbol: target
                for target in targets
            }
            active_targets[arm_id.value] = target_map
            for symbol, quantity in state.positions.items():
                if quantity <= 0:
                    continue
                target = target_map.get(symbol)
                if (
                    target is None
                    or quantity > target.target_quantity
                    or target.trigger_price is None
                ):
                    risk_required_symbols.add(symbol)
        quotes: dict[str, DecisionQuote] = {}
        for symbol in sorted(risk_required_symbols):
            quotes.update(
                self._fresh_decision_quotes(
                    symbols=(symbol,),
                    as_of=now,
                )
            )
        unavailable_optional_symbols: list[str] = []
        for symbol in all_held_symbols:
            if symbol in quotes:
                continue
            try:
                quotes.update(
                    self._fresh_decision_quotes(
                        symbols=(symbol,),
                        as_of=now,
                    )
                )
            except Q1CycleNotReady:
                unavailable_optional_symbols.append(symbol)
        fresh_prices = {
            symbol: quote.midpoint
            for symbol, quote in quotes.items()
        }
        valuation_prices: dict[str, dict[str, Decimal]] = {}
        frozen_achieved_target_valuations: dict[
            str,
            dict[str, dict[str, str]],
        ] = {}
        for arm_id, state in states.items():
            arm_prices = dict(fresh_prices)
            frozen: dict[str, dict[str, str]] = {}
            target_map = active_targets.get(arm_id, {})
            for symbol, quantity in state.positions.items():
                if quantity <= 0 or symbol in arm_prices:
                    continue
                target = target_map.get(symbol)
                if (
                    target is None
                    or quantity > target.target_quantity
                    or target.trigger_price is None
                ):
                    continue
                arm_prices[symbol] = target.trigger_price
                frozen[symbol] = {
                    "target_id": str(target.target_id),
                    "trigger_quote_id": target.trigger_quote_id,
                    "trigger_price": str(target.trigger_price),
                }
            valuation_prices[arm_id] = arm_prices
            if frozen:
                frozen_achieved_target_valuations[arm_id] = frozen
        quote_manifest_hash = canonical_hash(
            {
                "fresh_quotes": {
                    symbol: {
                        "quote_id": quote.quote_id,
                        "available_at": quote.available_at,
                        "midpoint": quote.midpoint,
                    }
                    for symbol, quote in sorted(quotes.items())
                },
                "frozen_achieved_target_valuations": (
                    frozen_achieved_target_valuations
                ),
            }
        )
        navs = {
            arm_id: state.nav(valuation_prices[arm_id])
            for arm_id, state in states.items()
            if all(
                quantity <= 0 or symbol in valuation_prices[arm_id]
                for symbol, quantity in state.positions.items()
            )
        }
        reconciliations = {
            arm_id.value: self._reconcile_state(
                run_id=cycle.run_id,
                arm_id=arm_id.value,
                state=states[arm_id.value],
                as_of=now,
            )
            for arm_id in (
                Q1ArmId.LIVE_MIRROR,
                Q1ArmId.Q1_DET,
                Q1ArmId.Q1_LLM,
            )
            if arm_id.value in states
        }
        source_manifest_hash = canonical_hash(
            {
                "calendar_session_id": calendar.calendar_session_id,
                "quote_manifest_hash": quote_manifest_hash,
                "risk_required_symbols": sorted(risk_required_symbols),
                "all_held_symbols": all_held_symbols,
                "unavailable_optional_symbols": (
                    unavailable_optional_symbols
                ),
                "frozen_achieved_target_valuations": (
                    frozen_achieved_target_valuations
                ),
                "reconciliation_result_hashes": {
                    arm_id: result.result_hash
                    for arm_id, result in sorted(
                        reconciliations.items()
                    )
                },
            }
        )
        risk_prepared: dict[str, dict[str, Any]] = {}
        config = risk_engine_config(self._runtime.config)
        for arm_id in (
            Q1ArmId.LIVE_MIRROR,
            Q1ArmId.Q1_DET,
            Q1ArmId.Q1_LLM,
        ):
            state = states.get(arm_id.value)
            if state is None:
                continue
            if arm_id.value not in navs:
                raise Q1CycleNotReady(
                    f"NAV valuation is incomplete for {arm_id.value}"
                )
            arm_prices = valuation_prices[arm_id.value]
            current_nav = navs[arm_id.value]
            reconciliation = reconciliations[arm_id.value]
            reconciliation_status = _reconciliation_status(
                reconciliation
            )
            session_open, running_peak = self._risk_nav_baselines(
                run_id=cycle.run_id,
                arm_id=arm_id.value,
                calendar_session_id=calendar.calendar_session_id,
                current_nav=current_nav,
            )
            check = RiskCheckInput(
                arm_id=arm_id,
                calendar_session_id=calendar.calendar_session_id,
                scheduled_at=_aware(cycle.scheduled_at),
                decision_created_at=now,
                positions=state.positions,
                settled_cash_usd=state.settled_cash_usd,
                unsettled_receivables_usd=state.unsettled_cash_usd,
                quotes=self._risk_check_quotes(
                    arm_id=arm_id.value,
                    state=state,
                    fresh_quotes=quotes,
                    active_targets=active_targets.get(
                        arm_id.value,
                        {},
                    ),
                ),
                session_open_nav_usd=session_open,
                running_peak_nav_usd=running_peak,
                portfolio_annualized_vol=(
                    self._current_portfolio_annualized_volatility(
                        cycle.run_id,
                        arm_id.value,
                        state=state,
                        prices=arm_prices,
                    )
                ),
                reconciliation_ok=reconciliation.ok,
                critical_reconciliation_condition=(
                    reconciliation.is_critical(
                        critical_reconciliation_conditions(
                            self._runtime.config
                        )
                    )
                ),
                reconciliation_status=reconciliation_status,
            )
            metrics = evaluate_risk_check(check, config)
            active_episode, existing_events = active_risk[arm_id.value]
            if arm_id.value in frozen_achieved_target_valuations:
                if active_episode is None:
                    raise Q1CycleError(
                        "Frozen target valuation requires an active episode"
                    )
                active_severity = (
                    active_episode.severity
                    if not existing_events
                    else existing_events[-1].severity
                )
                metrics = replace(
                    metrics,
                    indicated_severity=(
                        RiskSeverity.CRITICAL_EXIT
                        if check.critical_reconciliation_condition
                        else active_severity
                    ),
                )
            provenance = RiskEpisodeProvenance(
                run_id=cycle.run_id,
                config_manifest_hash=self._runtime.config.manifest_hash,
                code_version=workspace_code_version(self._workspace_root),
                model_version=Q1_MODEL_VERSION,
                source_manifest_hash=source_manifest_hash,
                worker_fence_token=_lease_owner(cycle),
                cycle_attempt_count=cycle.attempt_count,
            )
            transition = plan_risk_transition(
                check=check,
                metrics=metrics,
                config=config,
                provenance=provenance,
                active_episode=active_episode,
                existing_episode_events=existing_events,
                is_next_session_strategic_cycle=False,
                consecutive_valid_release_checks=0,
                source_cycle_id=cycle.cycle_id,
            )
            progress = (
                ()
                if transition.active_episode is None
                else target_progress_events(
                    episode=transition.active_episode,
                    existing_events=(
                        *existing_events,
                        *transition.new_events,
                    ),
                    positions=state.positions,
                    check=check,
                    provenance=provenance,
                    source_cycle_id=cycle.cycle_id,
                )
            )
            planned_decision = None
            planned_orders = None
            residual = {
                target.symbol: target.target_quantity
                for target in transition.executable_residual_targets
            }
            if residual and now < calendar.close_at:
                book = self._read_order_book(cycle.run_id, arm_id.value)
                pending = pending_orders(book.descriptors, book.events)
                adjusted_targets: dict[str, Decimal] = {}
                for symbol, target_quantity in residual.items():
                    required_sell = (
                        state.positions.get(symbol, Decimal("0"))
                        - target_quantity
                    )
                    already_pending = sum(
                        (
                            order.remaining_quantity
                            for order in pending
                            if (
                                order.order.symbol == symbol
                                and order.order.side is OrderSide.SELL
                            )
                        ),
                        Decimal("0"),
                    )
                    additional = max(
                        Decimal("0"),
                        required_sell - already_pending,
                    )
                    if additional > 0:
                        adjusted_targets[symbol] = (
                            state.positions[symbol] - additional
                        )
                if adjusted_targets:
                    current_weights = _general_weights(
                        state,
                        arm_prices,
                    )
                    latched_weights = _target_quantity_weights(
                        state=state,
                        target_quantities=residual,
                        prices=arm_prices,
                    )
                    refs = tuple(
                        PointInTimeSourceReference(
                            record_id=quotes[symbol].quote_id,
                            available_at=quotes[symbol].available_at,
                        )
                        for symbol in sorted(adjusted_targets)
                    )
                    manifest_content = {
                        "calendar_session_id": calendar.calendar_session_id,
                        "source_bars": (),
                        "quotes": refs,
                        "config_manifest_hash": (
                            self._runtime.config.manifest_hash
                        ),
                        "code_version": workspace_code_version(
                            self._workspace_root
                        ),
                        "model_version": Q1_MODEL_VERSION,
                        "source_manifest_hash": source_manifest_hash,
                    }
                    manifest = Q1DecisionInputManifest(
                        calendar_session_id=calendar.calendar_session_id,
                        source_bars=(),
                        quotes=refs,
                        config_manifest_hash=(
                            self._runtime.config.manifest_hash
                        ),
                        code_version=str(manifest_content["code_version"]),
                        model_version=Q1_MODEL_VERSION,
                        source_manifest_hash=source_manifest_hash,
                        manifest_hash=canonical_hash(manifest_content),
                    )
                    planned_decision = build_portfolio_decision(
                        run_id=cycle.run_id,
                        arm_id=arm_id,
                        source_cycle_id=cycle.cycle_id,
                        input_state_sequence=state.sequence,
                        decision_kind="EMERGENCY_REDUCTION",
                        scheduled_at=_aware(cycle.scheduled_at),
                        signal_data_cutoff=_aware(cycle.scheduled_at),
                        portfolio_state_as_of=now,
                        quote_as_of=max(
                            quotes[symbol].available_at
                            for symbol in adjusted_targets
                        ),
                        decision_created_at=now,
                        valid_until=calendar.close_at,
                        current_weights=current_weights,
                        deterministic_target_weights=latched_weights,
                        final_target_weights=latched_weights,
                        expected_annualized_volatility=Decimal("0"),
                        expected_one_way_turnover=_one_way_turnover(
                            current_weights,
                            latched_weights,
                        ),
                        used_daily_turnover_before=Decimal("0"),
                        signal_hash=None,
                        allocation_hash=None,
                        llm_overlay_state="DETERMINISTIC_RISK_PRECEDENCE",
                        llm_policy_id=None,
                        diagnostics={
                            "risk_state": (
                                transition.effective_severity.value
                            ),
                            "daily_loss": metrics.daily_loss,
                            "run_drawdown": metrics.run_drawdown,
                            "soft_daily": metrics.soft_daily_threshold,
                            "hard_daily": metrics.hard_daily_threshold,
                            "latched_target_quantities": residual,
                        },
                        input_manifest=manifest,
                        worker_fence_token=_lease_owner(cycle),
                        cycle_attempt_count=cycle.attempt_count,
                    )
                    planned_orders = plan_target_quantity_sell_orders(
                        decision=planned_decision,
                        current_positions=state.positions,
                        target_quantities=adjusted_targets,
                        quotes={
                            symbol: quotes[symbol]
                            for symbol in adjusted_targets
                        },
                        source_cycle_id=cycle.cycle_id,
                        input_state_sequence=state.sequence,
                        order_class=Q1OrderClass.EMERGENCY_REDUCTION,
                        config=order_planning_config(
                            self._runtime.config
                        ),
                    )
            risk_prepared[arm_id.value] = {
                "state_sequence": state.sequence,
                "metrics": metrics,
                "transition": transition,
                "progress": progress,
                "active_episode_id": (
                    None
                    if active_episode is None
                    else active_episode.risk_episode_id
                ),
                "decision": planned_decision,
                "orders": planned_orders,
                "reconciliation": reconciliation,
            }

        def writer(session: Session) -> dict[str, Any]:
            nav_ids: list[str] = []
            order_event_repository = OrderEventRepository(session)
            risk_repository = RiskEpisodeRepository(session)
            event_provenance = self._order_event_provenance(
                cycle,
                source_manifest_hash,
            )
            created_order_ids: list[str] = []
            risk_events: list[str] = []
            for arm_id, state in states.items():
                actual = latest_arm_state(
                    session,
                    run_id=cycle.run_id,
                    arm_id=arm_id,
                    lock=True,
                )
                if actual is None or actual.sequence != state.sequence:
                    raise Q1CycleError(
                        f"{arm_id} changed during NAV preparation"
                    )
                if arm_id not in navs:
                    continue
                nav = navs[arm_id]
                weights = _general_weights(
                    state,
                    valuation_prices[arm_id],
                )
                risk_payload = risk_prepared.get(arm_id)
                row = append_nav_snapshot(
                    session,
                    run_id=cycle.run_id,
                    arm_id=arm_id,
                    source_cycle_id=cycle.cycle_id,
                    as_of=now,
                    nav_usd=nav,
                    payload={
                        "schema_version": "q1_nav_v1",
                        "calendar_session_id": (
                            calendar.calendar_session_id
                        ),
                        "settled_cash_usd": str(
                            state.settled_cash_usd
                        ),
                        "unsettled_receivables_usd": str(
                            state.unsettled_cash_usd
                        ),
                        "positions_market_value_usd": str(
                            nav - state.total_cash_usd
                        ),
                        "actual_weights": {
                            symbol: str(value)
                            for symbol, value in weights.items()
                        },
                        "risk_state": (
                            "NOT_APPLICABLE"
                            if risk_payload is None
                            else risk_payload[
                                "transition"
                            ].effective_severity.value
                        ),
                        "release_condition_valid": (
                            False
                            if risk_payload is None
                            else _release_condition_valid(
                                risk_payload["metrics"],
                                config,
                            )
                        ),
                        "reconciliation_ok": (
                            True
                            if risk_payload is None
                            else risk_payload[
                                "reconciliation"
                            ].ok
                        ),
                        "reconciliation_status": (
                            "NOT_APPLICABLE"
                            if risk_payload is None
                            else _reconciliation_status(
                                risk_payload["reconciliation"]
                            )
                        ),
                        "reconciliation_result_hash": (
                            None
                            if risk_payload is None
                            else risk_payload[
                                "reconciliation"
                            ].result_hash
                        ),
                        "frozen_achieved_target_valuations": (
                            frozen_achieved_target_valuations.get(
                                arm_id,
                                {},
                            )
                        ),
                        "real_order_routing": False,
                    },
                    quote_manifest_hash=quote_manifest_hash,
                    algorithm_version="q1_math_core_v1",
                    config_manifest_hash=(
                        self._runtime.config.manifest_hash
                    ),
                    code_version=workspace_code_version(
                        self._workspace_root
                    ),
                    model_version=Q1_MODEL_VERSION,
                    source_manifest_hash=source_manifest_hash,
                )
                nav_ids.append(row.nav_snapshot_id)
            for arm_id, prepared in risk_prepared.items():
                book = load_q1_order_book(
                    session,
                    run_id=cycle.run_id,
                    arm_id=arm_id,
                )
                transition = prepared["transition"]
                if transition.cancel_pending_buys:
                    for event in soft_stop_buy_cancellations(
                        orders=book.descriptors,
                        events=book.events,
                        occurred_at=now,
                        available_at=now,
                        provenance=event_provenance,
                        source_cycle_id=cycle.cycle_id,
                    ):
                        order_event_repository.append(event)
                        risk_events.append(event.event_id)
                new_episode = transition.new_episode
                new_events = tuple(transition.new_events)
                if new_episode is not None:
                    activation = new_events[0]
                    risk_repository.append_episode(
                        new_episode,
                        activation,
                    )
                    risk_events.append(activation.risk_episode_event_id)
                    new_events = new_events[1:]
                for event in (*new_events, *prepared["progress"]):
                    risk_repository.append_event(event)
                    risk_events.append(event.risk_episode_event_id)
                decision = prepared["decision"]
                planned = prepared["orders"]
                if decision is None or planned is None:
                    continue
                append_strategy_decision(session, decision=decision)
                append_risk_approval(
                    session,
                    risk_decision_id=risk_approval_id(decision),
                    decision=decision,
                )
                for intent in planned.intents:
                    append_order_intent(session, intent)
                    session.flush()
                    order_event_repository.append(
                        append_order_event(
                            order=_descriptor(intent),
                            existing_events=(),
                            event_type=OrderEventType.CREATED,
                            occurred_at=now,
                            available_at=now,
                            provenance=event_provenance,
                            source_cycle_id=cycle.cycle_id,
                        )
                    )
                    created_order_ids.append(intent.order_intent_id)
            return {
                "status": "NAV_AND_DETERMINISTIC_RISK_RECORDED",
                "nav_snapshot_ids": nav_ids,
                "risk_event_ids": risk_events,
                "emergency_order_intent_ids": created_order_ids,
                "real_order_routing": False,
            }

        return self._commit_cycle(
            cycle,
            cutoff=now,
            input_manifest={
                "cycle_id": cycle.cycle_id,
                "calendar_session_id": calendar.calendar_session_id,
                "quote_manifest_hash": quote_manifest_hash,
                "source_manifest_hash": source_manifest_hash,
                "config_manifest_hash": self._runtime.config.manifest_hash,
                "real_order_routing": False,
            },
            writer=writer,
            now=now,
        )

    def _process_llm_review(self, cycle: PaperCycleRow) -> dict[str, Any]:
        from trading.runtime.q1_llm_review import (
            Q1LlmReviewCycleProcessor,
        )

        return Q1LlmReviewCycleProcessor(
            self._session_factory,
            runtime=self._runtime,
            workspace_root=self._workspace_root,
            llm_overlay_provider=self._llm_overlay_provider,
            news_refresher=self._llm_news_refresher,
            clock=self._clock,
        ).process(cycle)

    def _process_execution(self, cycle: PaperCycleRow) -> dict[str, Any]:
        return Q1ExecutionCycleProcessor(
            self._session_factory,
            runtime=self._runtime,
            workspace_root=self._workspace_root,
        ).process(
            cycle,
            calendar=self._calendar_for_cycle(cycle),
            now=self._clock.now(),
        )

    def _process_daily_result(self, cycle: PaperCycleRow) -> dict[str, Any]:
        try:
            return Q1EvaluationCycleProcessor(
                self._session_factory,
                runtime=self._runtime,
                workspace_root=self._workspace_root,
            ).process(
                cycle,
                calendar=self._calendar_for_cycle(cycle),
                now=self._clock.now(),
            )
        except Q1EvaluationDataNotReady as error:
            raise Q1CycleNotReady(str(error)) from error

    def _effect_only(self, cycle: PaperCycleRow, status: str) -> dict[str, Any]:
        now = self._clock.now()
        return self._commit_cycle(
            cycle,
            cutoff=now,
            input_manifest={
                "cycle_id": cycle.cycle_id,
                "scheduled_at": _aware(cycle.scheduled_at),
                "config_manifest_hash": self._runtime.config.manifest_hash,
                "real_order_routing": False,
            },
            writer=lambda _session: {
                "status": status,
                "orders_created": 0,
                "real_order_routing": False,
            },
            now=now,
        )


def _general_weights(
    state: Q1ArmState,
    prices: Mapping[str, Decimal],
) -> dict[str, Decimal]:
    nav = state.nav(dict(prices))
    if nav <= 0:
        raise Q1CycleError("Portfolio NAV must be positive")
    weights = {
        symbol: quantity * prices[symbol] / nav
        for symbol, quantity in sorted(state.positions.items())
        if quantity > 0
    }
    risky_weight = sum(weights.values(), Decimal("0"))
    if risky_weight > 1:
        raise Q1CycleError("Portfolio weights imply leverage")
    weights["USD_CASH"] = Decimal("1") - risky_weight
    return weights


def _target_quantity_weights(
    *,
    state: Q1ArmState,
    target_quantities: Mapping[str, Decimal],
    prices: Mapping[str, Decimal],
) -> dict[str, Decimal]:
    unknown = sorted(set(target_quantities) - set(state.positions))
    if unknown:
        raise Q1CycleError(f"Risk target references absent positions: {unknown}")
    nav = state.nav(dict(prices))
    if nav <= 0:
        raise Q1CycleError("Portfolio NAV must be positive")
    weights: dict[str, Decimal] = {}
    for symbol, current_quantity in sorted(state.positions.items()):
        target_quantity = target_quantities.get(symbol, current_quantity)
        if target_quantity < 0 or target_quantity > current_quantity:
            raise Q1CycleError("Risk target quantity is outside current holdings")
        if target_quantity > 0:
            weights[symbol] = target_quantity * prices[symbol] / nav
    risky_weight = sum(weights.values(), Decimal("0"))
    if risky_weight > 1:
        raise Q1CycleError("Latched risk targets imply leverage")
    weights["USD_CASH"] = Decimal("1") - risky_weight
    return weights


def _one_way_turnover(
    current_weights: Mapping[str, Decimal],
    target_weights: Mapping[str, Decimal],
) -> Decimal:
    symbols = set(current_weights) | set(target_weights)
    return Decimal("0.5") * sum(
        (
            abs(
                target_weights.get(symbol, Decimal("0"))
                - current_weights.get(symbol, Decimal("0"))
            )
            for symbol in symbols
        ),
        Decimal("0"),
    )


def _release_condition_valid(
    metrics: RiskMetrics,
    config: RiskEngineConfig,
) -> bool:
    return (
        metrics.daily_loss
        < config.release_daily_loss_soft_fraction
        * metrics.soft_daily_threshold
        and metrics.run_drawdown < config.release_drawdown_threshold
    )


def _descriptor(intent: Q1OrderIntent) -> Any:
    from trading.execution.order_state import OrderDescriptor, Q1OrderClass

    return OrderDescriptor(
        order_intent_id=intent.order_intent_id,
        arm_id=intent.arm_id.value,
        portfolio_decision_id=intent.portfolio_decision_id,
        symbol=intent.symbol,
        side=intent.side,
        quantity=intent.quantity,
        order_class=Q1OrderClass(intent.order_class),
        created_at=intent.created_at,
        valid_until=intent.valid_until,
    )


def _signal_diagnostics(signal: Q1Signal) -> dict[str, Any]:
    return cast(
        dict[str, JsonValue],
        canonical_data({
            "covariance": signal.covariance.as_mapping(),
            "trend_z_scores": {
                trend.symbol: dict(trend.z_scores_by_horizon)
                for trend in signal.trends
            },
            "T_QQQ": signal.trend_for("QQQ").trend_score,
            "T_SOXX": signal.trend_for("SOXX").trend_score,
            "RS": signal.relative_strength,
            "market_gate": signal.market_gate,
            "raw_scores": dict(signal.raw_scores),
            "confidence": signal.confidence,
        }),
    )


def _tradable_usd_cash(account: PaperAccountSpec) -> Decimal:
    return next(
        item.amount
        for item in account.cash
        if item.currency == "USD" and item.tradable
    )


def _lease_owner(cycle: PaperCycleRow) -> str:
    if not cycle.lease_owner:
        raise Q1CycleError("Q1 cycle has no lease owner")
    return cycle.lease_owner


def _reconciliation_status(
    result: Q1ReconciliationResult,
) -> str:
    return "|".join(
        condition.value
        for condition in result.conditions
    )


def _provider_audit_payload(
    provider: object,
    request_id: str,
) -> dict[str, object] | None:
    accessor = getattr(provider, "audit_for_request", None)
    if not callable(accessor):
        return None
    try:
        records: object = accessor(request_id)
        if not isinstance(records, (tuple, list)) or not records:
            return None
        typed_records = cast(tuple[object, ...] | list[object], records)
        serializer = getattr(typed_records[-1], "as_payload", None)
        if not callable(serializer):
            return None
        payload = serializer()
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return {
        str(key): value
        for key, value in cast(dict[object, object], payload).items()
    }


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
