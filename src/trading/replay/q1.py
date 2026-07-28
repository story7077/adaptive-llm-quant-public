from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trading.domain.contracts import Fill, model_payload
from trading.domain.enums import OrderSide
from trading.domain.hashing import canonical_hash, stable_id
from trading.domain.q1 import (
    CashSettlementEvent,
    CashSettlementEventType,
    MatchedAttributionResult,
    MatchedComparison,
    OrderEvent,
    OrderEventType,
    Q1ArmId,
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
    OrderAggregate,
    Q1OrderClass,
    validate_order_event_book,
)
from trading.persistence.models import (
    ArmStateSnapshotRow,
    CashSettlementEventRow,
    FillRow,
    MatchedAttributionResultRow,
    NavSnapshotRow,
    OrderEventRow,
    OrderIntentRow,
    PortfolioDecisionRow,
    RiskEpisodeEventRow,
    RiskEpisodeRow,
    RiskEpisodeTargetRow,
    RunRow,
    StrategyDailyResultRow,
    StrategyEvaluationAnchorRow,
)
from trading.persistence.q1_runtime import load_q1_order_book
from trading.runtime.q1_state import Q1ArmState, UnsettledReceivable
from trading.settlement.service import apply_settlement_events

Q1_ALGORITHM_VERSION = "q1_math_core_v1"


@dataclass(frozen=True, slots=True)
class Q1ReplayResult:
    run_id: str
    mode: str
    manifest: dict[str, Any]
    result_hash: str
    checks: dict[str, bool]

    @property
    def passed(self) -> bool:
        return all(self.checks.values())

    def as_payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "algorithm_version": Q1_ALGORITHM_VERSION,
            "mode": self.mode,
            "passed": self.passed,
            "checks": self.checks,
            "result_hash": self.result_hash,
            "manifest": self.manifest,
            "real_order_routing": False,
        }


@dataclass(frozen=True, slots=True)
class _StateTransition:
    transition_id: str
    arm_id: str
    source_cycle_id: str
    created_at: datetime
    fill: Q1Fill | None = None
    settlement: CashSettlementEvent | None = None


def replay_q1_run(
    session_factory: sessionmaker[Session],
    run_id: str,
) -> Q1ReplayResult:
    """Replay and verify the immutable Q1 economic record streams.

    Operational lease tokens and retry counts remain in audit payloads but are
    deliberately excluded from economic decision identities. The returned
    replay hash depends only on ordered immutable record hashes and reconstructed
    arm-state/NAV hashes.
    """

    with session_factory() as session:
        run = session.get(RunRow, run_id)
        if run is None:
            raise ValueError(f"Unknown run_id: {run_id}")
        if run.experiment_version != Q1_ALGORITHM_VERSION:
            raise ValueError(
                f"Run {run_id!r} belongs to {run.experiment_version}, "
                f"not {Q1_ALGORITHM_VERSION}"
            )

        decisions, decision_rows = _load_models(
            session,
            PortfolioDecisionRow,
            Q1StrategyDecision,
            run_id=run_id,
            order_by=(
                PortfolioDecisionRow.decision_time,
                PortfolioDecisionRow.portfolio_decision_id,
            ),
        )
        intents, intent_rows = _load_models(
            session,
            OrderIntentRow,
            Q1OrderIntent,
            run_id=run_id,
            order_by=(OrderIntentRow.created_at, OrderIntentRow.order_intent_id),
        )
        order_events, order_event_rows = _load_models(
            session,
            OrderEventRow,
            OrderEvent,
            run_id=run_id,
            order_by=(OrderEventRow.order_intent_id, OrderEventRow.event_sequence),
        )
        fills, fill_rows = _load_models(
            session,
            FillRow,
            Q1Fill,
            run_id=run_id,
            order_by=(FillRow.effective_at, FillRow.fill_id),
        )
        risk_episodes, risk_episode_rows = _load_models(
            session,
            RiskEpisodeRow,
            RiskEpisode,
            run_id=run_id,
            order_by=(RiskEpisodeRow.triggered_at, RiskEpisodeRow.risk_episode_id),
        )
        risk_events, risk_event_rows = _load_models(
            session,
            RiskEpisodeEventRow,
            RiskEpisodeEvent,
            run_id=run_id,
            order_by=(
                RiskEpisodeEventRow.risk_episode_id,
                RiskEpisodeEventRow.event_sequence,
            ),
        )
        settlement_events, settlement_rows = _load_models(
            session,
            CashSettlementEventRow,
            CashSettlementEvent,
            run_id=run_id,
            order_by=(
                CashSettlementEventRow.effective_at,
                CashSettlementEventRow.cash_settlement_event_id,
            ),
        )
        daily_results, daily_rows = _load_models(
            session,
            StrategyDailyResultRow,
            StrategyDailyResult,
            run_id=run_id,
            order_by=(
                StrategyDailyResultRow.session_date,
                StrategyDailyResultRow.arm_id,
            ),
        )
        matched_results, matched_rows = _load_models(
            session,
            MatchedAttributionResultRow,
            MatchedAttributionResult,
            run_id=run_id,
            order_by=(
                MatchedAttributionResultRow.through_session_date,
                MatchedAttributionResultRow.comparison,
            ),
        )
        anchor_rows = tuple(
            session.scalars(
                select(StrategyEvaluationAnchorRow)
                .where(
                    StrategyEvaluationAnchorRow.run_id == run_id,
                    StrategyEvaluationAnchorRow.algorithm_version
                    == Q1_ALGORITHM_VERSION,
                )
                .order_by(StrategyEvaluationAnchorRow.evaluation_anchor_id)
            )
        )
        anchors = tuple(
            StrategyEvaluationAnchor.model_validate(row.payload_json)
            for row in anchor_rows
        )
        target_rows = tuple(
            session.scalars(
                select(RiskEpisodeTargetRow)
                .join(
                    RiskEpisodeRow,
                    RiskEpisodeRow.risk_episode_id
                    == RiskEpisodeTargetRow.risk_episode_id,
                )
                .where(
                    RiskEpisodeRow.run_id == run_id,
                    RiskEpisodeRow.algorithm_version == Q1_ALGORITHM_VERSION,
                )
                .order_by(
                    RiskEpisodeTargetRow.risk_episode_id,
                    RiskEpisodeTargetRow.target_generation,
                    RiskEpisodeTargetRow.symbol,
                )
            )
        )
        state_rows = tuple(
            session.scalars(
                select(ArmStateSnapshotRow)
                .where(ArmStateSnapshotRow.run_id == run_id)
                .order_by(
                    ArmStateSnapshotRow.arm_id,
                    ArmStateSnapshotRow.sequence,
                    ArmStateSnapshotRow.arm_state_snapshot_id,
                )
            )
        )
        nav_rows = tuple(
            session.scalars(
                select(NavSnapshotRow)
                .where(
                    NavSnapshotRow.run_id == run_id,
                    NavSnapshotRow.algorithm_version == Q1_ALGORITHM_VERSION,
                )
                .order_by(
                    NavSnapshotRow.as_of,
                    NavSnapshotRow.arm_id,
                    NavSnapshotRow.nav_snapshot_id,
                )
            )
        )
        order_book = load_q1_order_book(session, run_id=run_id)

    order_aggregates, order_states_valid = _validated_order_book(
        order_book.descriptors,
        order_book.events,
    )
    state_hashes, states_reconstructed, states_by_sequence = _state_hashes(
        state_rows,
        fills=fills,
        settlement_events=settlement_events,
    )
    nav_hashes, navs_reconstructed = _nav_hashes(
        nav_rows,
        state_rows=state_rows,
        states_by_sequence=states_by_sequence,
    )
    complete_session_records = _complete_record_set_present(
        anchors=anchors,
        decisions=decisions,
        state_rows=state_rows,
        nav_rows=nav_rows,
        settlement_events=settlement_events,
        daily_results=daily_results,
        matched_results=matched_results,
    )
    decision_hashes = tuple(item.decision_hash for item in decisions)
    intent_hashes = tuple(item.intent_hash for item in intents)
    order_event_hashes = tuple(item.event_hash for item in order_events)
    fill_hashes = tuple(item.fill_hash for item in fills)
    risk_episode_hashes = tuple(item.episode_hash for item in risk_episodes)
    risk_event_hashes = tuple(item.event_hash for item in risk_events)
    risk_target_hashes = tuple(row.target_hash for row in target_rows)
    settlement_hashes = tuple(item.event_hash for item in settlement_events)
    daily_hashes = tuple(item.result_hash for item in daily_results)
    matched_hashes = tuple(item.result_hash for item in matched_results)
    anchor_hashes = tuple(item.anchor_hash for item in anchors)

    manifest = {
        "schema_version": "q1_replay_manifest_v2",
        "run_id": run_id,
        "algorithm_version": Q1_ALGORITHM_VERSION,
        "config_manifest_hash": run.config_manifest_hash,
        "code_version": run.code_commit,
        "evaluation_anchor_hashes": anchor_hashes,
        "decision_hashes": decision_hashes,
        "intent_hashes": intent_hashes,
        "order_event_hashes": order_event_hashes,
        "fill_hashes": fill_hashes,
        "state_hashes": state_hashes,
        "nav_hashes": nav_hashes,
        "risk_episode_hashes": risk_episode_hashes,
        "risk_target_hashes": risk_target_hashes,
        "risk_event_hashes": risk_event_hashes,
        "cash_settlement_event_hashes": settlement_hashes,
        "daily_result_hashes": daily_hashes,
        "matched_attribution_hashes": matched_hashes,
        "latest_order_states": tuple(
            {
                "order_intent_id": item.order.order_intent_id,
                "status": item.status.value,
                "remaining_quantity": item.remaining_quantity,
                "cumulative_filled_quantity": item.cumulative_filled_quantity,
                "cumulative_commission_usd": item.cumulative_commission_usd,
            }
            for item in order_aggregates
        ),
        "real_order_routing": False,
    }
    checks = {
        "run_is_q1_math_core_v1": run.experiment_version == Q1_ALGORITHM_VERSION,
        "real_order_routing_false": (
            isinstance(run.result_manifest, dict)
            and run.result_manifest.get("real_order_routing") is False
        ),
        "complete_session_record_set_present": complete_session_records,
        "anchor_hashes_valid": _anchor_hashes_valid(anchors, anchor_rows),
        "initial_state_economics_valid": _initial_state_economics_valid(
            anchors=anchors,
            states_by_sequence=states_by_sequence,
            settlement_events=settlement_events,
        ),
        "decision_hashes_valid": all(
            _decision_hash(item) == item.decision_hash for item in decisions
        ),
        "decision_identities_valid": all(
            stable_id(
                "q1-portfolio-decision",
                item.run_id,
                item.arm_id,
                item.scheduled_at,
                item.decision_hash,
            )
            == item.portfolio_decision_id
            for item in decisions
        ),
        "decision_rows_consistent": _decision_rows_consistent(
            decisions,
            decision_rows,
        ),
        "intent_hashes_valid": _intent_hashes_valid(
            intents,
            decisions=decisions,
            states_by_sequence=states_by_sequence,
        ),
        "intent_rows_consistent": _intent_rows_consistent(
            intents,
            intent_rows,
        ),
        "order_event_hashes_valid": all(
            _order_event_hash(item) == item.event_hash for item in order_events
        ),
        "order_event_rows_consistent": _order_event_rows_consistent(
            order_events,
            order_event_rows,
        ),
        "order_state_machine_valid": order_states_valid,
        "fill_hashes_valid": all(
            canonical_hash(item.model_dump(exclude={"fill_hash"}))
            == item.fill_hash
            for item in fills
        ),
        "fill_rows_consistent": _fill_rows_consistent(fills, fill_rows),
        "fill_order_event_economics_valid": _fill_order_event_economics_valid(
            fills,
            intents=intents,
            order_events=order_events,
        ),
        "cash_settlement_hashes_valid": all(
            _settlement_event_hash(item) == item.event_hash
            for item in settlement_events
        ),
        "cash_settlement_rows_consistent": _settlement_rows_consistent(
            settlement_events,
            settlement_rows,
        ),
        "cash_settlement_economics_valid": _cash_settlement_economics_valid(
            settlement_events,
            fills=fills,
        ),
        "risk_episode_hashes_valid": all(
            _risk_episode_hash(item) == item.episode_hash
            for item in risk_episodes
        ),
        "risk_episode_rows_consistent": _risk_episode_rows_consistent(
            risk_episodes,
            risk_episode_rows,
        ),
        "risk_event_hashes_valid": all(
            _risk_event_hash(item) == item.event_hash for item in risk_events
        ),
        "risk_event_rows_consistent": _risk_event_rows_consistent(
            risk_events,
            risk_event_rows,
        ),
        "typed_risk_targets_valid": _typed_risk_targets_valid(
            risk_episodes,
            risk_events=risk_events,
            risk_event_rows=risk_event_rows,
            target_rows=target_rows,
        ),
        "daily_result_hashes_valid": _daily_results_valid(
            daily_results,
            daily_rows,
        ),
        "matched_result_hashes_valid": _matched_results_valid(
            matched_results,
            matched_rows,
            daily_results=daily_results,
        ),
        "arm_states_reconstructed": states_reconstructed,
        "nav_economics_reconstructed": navs_reconstructed,
        # Backward-compatible check names retained for existing consumers.
        "state_hashes_valid": states_reconstructed,
        "nav_hashes_valid": navs_reconstructed,
        "config_manifest_consistent": _config_hashes_consistent(
            run.config_manifest_hash,
            (
                *anchors,
                *decisions,
                *intents,
                *order_events,
                *fills,
                *risk_episodes,
                *risk_events,
                *settlement_events,
                *daily_results,
                *matched_results,
            ),
        )
        and all(
            row.config_manifest_hash == run.config_manifest_hash
            for row in target_rows
        ),
    }
    return Q1ReplayResult(
        run_id=run_id,
        mode=(
            "FULL_EVENT_REPLAY"
            if complete_session_records
            else "INCOMPLETE_EVENT_STREAM"
        ),
        manifest=manifest,
        result_hash=canonical_hash(manifest),
        checks=checks,
    )


def _load_models(
    session: Session,
    row_type: type[Any],
    model_type: type[Any],
    *,
    run_id: str,
    order_by: tuple[Any, ...],
) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    rows = tuple(
        session.scalars(
            select(row_type)
            .where(
                row_type.run_id == run_id,
                row_type.algorithm_version == Q1_ALGORITHM_VERSION,
            )
            .order_by(*order_by)
        )
    )
    return (
        tuple(model_type.model_validate(row.payload_json) for row in rows),
        rows,
    )


def _decision_hash(item: Q1StrategyDecision) -> str:
    return canonical_hash(
        {
            "run_id": item.run_id,
            "arm_id": item.arm_id,
            "algorithm_version": item.algorithm_version,
            "source_cycle_id": item.source_cycle_id,
            "input_state_sequence": item.input_state_sequence,
            "decision_kind": item.decision_kind,
            "scheduled_at": item.scheduled_at,
            "signal_data_cutoff": item.signal_data_cutoff,
            "portfolio_state_as_of": item.portfolio_state_as_of,
            "quote_as_of": item.quote_as_of,
            "decision_created_at": item.decision_created_at,
            "valid_until": item.valid_until,
            "target_weights": item.target_weights,
            "diagnostics": item.diagnostics,
            "input_manifest": item.input_manifest,
        }
    )


def _complete_record_set_present(
    *,
    anchors: tuple[StrategyEvaluationAnchor, ...],
    decisions: tuple[Q1StrategyDecision, ...],
    state_rows: tuple[ArmStateSnapshotRow, ...],
    nav_rows: tuple[NavSnapshotRow, ...],
    settlement_events: tuple[CashSettlementEvent, ...],
    daily_results: tuple[StrategyDailyResult, ...],
    matched_results: tuple[MatchedAttributionResult, ...],
) -> bool:
    all_arms = {item.value for item in Q1ArmId}
    strategy_arms = {
        Q1ArmId.B0_CASH.value,
        Q1ArmId.B0_QQQ.value,
        Q1ArmId.B0_VOL.value,
        Q1ArmId.Q1_DET.value,
        Q1ArmId.Q1_LLM.value,
    }
    root_arms = {
        row.arm_id
        for row in state_rows
        if row.sequence == 0
    }
    nav_arms = {row.arm_id for row in nav_rows}
    opening_cash_arms = {
        item.arm_id.value
        for item in settlement_events
        if item.event_type is CashSettlementEventType.OPENING_SETTLED_CASH
    }
    decision_arms = {item.arm_id.value for item in decisions}
    sessions_by_arm: dict[object, set[str]] = {}
    for result in daily_results:
        sessions_by_arm.setdefault(result.session_date, set()).add(
            result.arm_id.value
        )
    has_complete_daily_session = any(
        arms == all_arms for arms in sessions_by_arm.values()
    )
    comparisons = {item.comparison for item in matched_results}
    return (
        len(anchors) == 1
        and root_arms == all_arms
        and nav_arms == all_arms
        and opening_cash_arms == all_arms
        and strategy_arms.issubset(decision_arms)
        and has_complete_daily_session
        and comparisons
        == {
            MatchedComparison.Q1_DET_MINUS_B0_VOL,
            MatchedComparison.Q1_LLM_MINUS_Q1_DET,
        }
    )


def _anchor_hashes_valid(
    anchors: tuple[StrategyEvaluationAnchor, ...],
    rows: tuple[StrategyEvaluationAnchorRow, ...],
) -> bool:
    if len(anchors) != len(rows):
        return False
    for item, row in zip(anchors, rows, strict=True):
        expected = canonical_hash(
            {
                "run_id": item.run_id,
                "calendar_session_id": item.calendar_session_id,
                "common_t0_at": item.common_t0_at,
                "initial_nav_usd": item.initial_nav_usd,
                "quote_manifest_hash": item.quote_manifest_hash,
                "config_manifest_hash": item.config_manifest_hash,
                "code_version": item.code_version,
                "model_version": item.model_version,
                "source_manifest_hash": item.source_manifest_hash,
            }
        )
        if not (
            expected == item.anchor_hash == row.anchor_hash
            and item.evaluation_anchor_id == row.evaluation_anchor_id
            and stable_id(
                "q1-evaluation-anchor",
                item.run_id,
                item.anchor_hash,
            )
            == item.evaluation_anchor_id
            and _payload_matches(row.payload_json, item)
        ):
            return False
    return True


def _initial_state_economics_valid(
    *,
    anchors: tuple[StrategyEvaluationAnchor, ...],
    states_by_sequence: dict[tuple[str, int], Q1ArmState],
    settlement_events: tuple[CashSettlementEvent, ...],
) -> bool:
    if len(anchors) != 1:
        return False
    anchor = anchors[0]
    roots = {
        arm.value: states_by_sequence.get((arm.value, 0))
        for arm in Q1ArmId
    }
    if any(state is None for state in roots.values()):
        return False
    opening_events: dict[str, list[CashSettlementEvent]] = {}
    for event in settlement_events:
        if event.event_type is CashSettlementEventType.OPENING_SETTLED_CASH:
            opening_events.setdefault(event.arm_id.value, []).append(event)
    if set(opening_events) != set(roots) or any(
        len(events) != 1 for events in opening_events.values()
    ):
        return False
    for arm_id, maybe_state in roots.items():
        if maybe_state is None:
            return False
        state = maybe_state
        opening = opening_events[arm_id][0]
        if not (
            state.sequence == 0
            and state.evaluation_anchor_id == anchor.evaluation_anchor_id
            and state.initial_nav_usd == anchor.initial_nav_usd
            and state.unsettled_receivables == ()
            and opening.effective_at == anchor.common_t0_at
            and opening.settled_cash_delta_usd
            == state.settled_cash_usd
            and opening.gross_amount_usd == state.settled_cash_usd
        ):
            return False
    strategy_arms = (
        Q1ArmId.B0_CASH.value,
        Q1ArmId.B0_QQQ.value,
        Q1ArmId.B0_VOL.value,
        Q1ArmId.Q1_DET.value,
        Q1ArmId.Q1_LLM.value,
    )
    for arm_id in strategy_arms:
        state = roots[arm_id]
        if state is None or not (
            state.positions == {}
            and state.settled_cash_usd == anchor.initial_nav_usd
        ):
            return False
    hold = roots[Q1ArmId.HOLD.value]
    live_mirror = roots[Q1ArmId.LIVE_MIRROR.value]
    return (
        hold is not None
        and live_mirror is not None
        and hold.positions == live_mirror.positions
        and hold.settled_cash_usd == live_mirror.settled_cash_usd
    )


def _decision_rows_consistent(
    items: tuple[Q1StrategyDecision, ...],
    rows: tuple[PortfolioDecisionRow, ...],
) -> bool:
    if len(items) != len(rows):
        return False
    return all(
        row.portfolio_decision_id == item.portfolio_decision_id
        and row.decision_hash == item.decision_hash
        and row.run_id == item.run_id
        and row.arm_id == item.arm_id.value
        and row.source_cycle_id == item.source_cycle_id
        and row.input_state_sequence == item.input_state_sequence
        and row.calendar_session_id == item.input_manifest.calendar_session_id
        and row.input_manifest_hash == item.input_manifest.manifest_hash
        and _payload_matches(row.payload_json, item)
        for item, row in zip(items, rows, strict=True)
    )


def _intent_hashes_valid(
    items: tuple[Q1OrderIntent, ...],
    *,
    decisions: tuple[Q1StrategyDecision, ...],
    states_by_sequence: dict[tuple[str, int], Q1ArmState],
) -> bool:
    decision_by_id = {
        item.portfolio_decision_id: item for item in decisions
    }
    for item in items:
        decision = decision_by_id.get(item.portfolio_decision_id)
        if (
            decision is None
            or decision.run_id != item.run_id
            or decision.arm_id is not item.arm_id
            or stable_id(
                "q1-risk-approval",
                decision.portfolio_decision_id,
                decision.target_weights,
            )
            != item.risk_decision_id
        ):
            return False
        expected = _intent_hash(
            item,
            states_by_sequence=states_by_sequence,
        )
        if (
            expected is None
            or expected != item.intent_hash
            or stable_id("q1-order-intent", expected)
            != item.order_intent_id
            or stable_id("q1-order-intent-idem", expected)
            != item.idempotency_key
        ):
            return False
    return True


def _intent_hash(
    item: Q1OrderIntent,
    *,
    states_by_sequence: dict[tuple[str, int], Q1ArmState],
) -> str | None:
    identity: dict[str, object] = {
        "run_id": item.run_id,
        "arm_id": item.arm_id,
        "portfolio_decision_id": item.portfolio_decision_id,
        "source_cycle_id": item.source_cycle_id,
        "symbol": item.symbol,
        "side": item.side,
        "quantity": item.quantity,
    }
    if item.order_class == Q1OrderClass.NORMAL.value:
        identity.update(
            {
                "decision_quote_id": item.decision_quote_id,
                "created_at": item.created_at,
                "valid_until": item.valid_until,
            }
        )
    else:
        state = states_by_sequence.get(
            (item.arm_id.value, item.input_state_sequence)
        )
        if state is None:
            return None
        target_quantity = (
            state.positions.get(item.symbol, Decimal("0")) - item.quantity
        )
        if target_quantity < 0:
            return None
        identity.update(
            {
                "target_quantity": target_quantity,
                "decision_quote_id": item.decision_quote_id,
                "created_at": item.created_at,
                "valid_until": item.valid_until,
                "order_class": item.order_class,
            }
        )
    return canonical_hash(
        {
            **identity,
            "risk_decision_id": item.risk_decision_id,
            "decision_reference_price": item.decision_reference_price,
            "decision_spread_bps": item.decision_spread_bps,
            "input_state_sequence": item.input_state_sequence,
            "config_manifest_hash": item.config_manifest_hash,
            "code_version": item.code_version,
            "model_version": item.model_version,
            "source_manifest_hash": item.source_manifest_hash,
        }
    )


def _intent_rows_consistent(
    items: tuple[Q1OrderIntent, ...],
    rows: tuple[OrderIntentRow, ...],
) -> bool:
    if len(items) != len(rows):
        return False
    return all(
        row.order_intent_id == item.order_intent_id
        and row.intent_hash == item.intent_hash
        and row.idempotency_key == item.idempotency_key
        and row.run_id == item.run_id
        and row.arm_id == item.arm_id.value
        and row.symbol == item.symbol
        and row.side == item.side.value
        and row.quantity == item.quantity
        and _payload_matches(row.payload_json, item)
        for item, row in zip(items, rows, strict=True)
    )


def _order_event_hash(item: OrderEvent) -> str:
    return canonical_hash(
        {
            "order_intent_id": item.order_intent_id,
            "event_type": item.event_type,
            "event_sequence": item.event_sequence,
            "quantity_delta": item.quantity_delta,
            "commission_delta_usd": item.commission_delta_usd,
            "remaining_quantity": item.remaining_quantity,
            "occurred_at": item.occurred_at,
            "source_id": item.source_id,
            "quote_id": item.quote_id,
            "source_cycle_id": item.source_cycle_id,
            "cumulative_filled_quantity": item.cumulative_filled_quantity,
            "cumulative_commission_usd": item.cumulative_commission_usd,
            "reason": item.reason,
            "config_manifest_hash": item.config_manifest_hash,
            "code_version": item.code_version,
            "model_version": item.model_version,
            "source_manifest_hash": item.source_manifest_hash,
        }
    )


def _order_event_rows_consistent(
    items: tuple[OrderEvent, ...],
    rows: tuple[OrderEventRow, ...],
) -> bool:
    if len(items) != len(rows):
        return False
    return all(
        row.order_event_id == item.event_id
        and row.event_hash == item.event_hash
        and row.idempotency_key == item.idempotency_key
        and row.order_intent_id == item.order_intent_id
        and row.event_sequence == item.event_sequence
        and row.event_type == item.event_type.value
        and row.remaining_quantity == item.remaining_quantity
        and row.cumulative_filled_quantity
        == item.cumulative_filled_quantity
        and row.cumulative_commission_usd
        == item.cumulative_commission_usd
        and stable_id("q1-order-event-idem", item.event_id)
        == item.idempotency_key
        and _payload_matches(row.payload_json, item)
        for item, row in zip(items, rows, strict=True)
    )


def _validated_order_book(
    descriptors: tuple[Any, ...],
    events: tuple[OrderEvent, ...],
) -> tuple[tuple[OrderAggregate, ...], bool]:
    try:
        return validate_order_event_book(descriptors, events), True
    except ValueError:
        return (), False


def _fill_rows_consistent(
    items: tuple[Q1Fill, ...],
    rows: tuple[FillRow, ...],
) -> bool:
    if len(items) != len(rows):
        return False
    return all(
        row.fill_id == item.fill_id
        and row.fill_hash == item.fill_hash
        and row.order_intent_id == item.order_intent_id
        and row.run_id == item.run_id
        and row.arm_id == item.arm_id.value
        and row.quote_id == item.quote_id
        and row.symbol == item.symbol
        and row.side == item.side.value
        and row.quantity == item.quantity
        and row.price == item.price
        and row.commission_usd == item.commission_usd
        and _payload_matches(row.payload_json, item)
        for item, row in zip(items, rows, strict=True)
    )


def _fill_order_event_economics_valid(
    fills: tuple[Q1Fill, ...],
    *,
    intents: tuple[Q1OrderIntent, ...],
    order_events: tuple[OrderEvent, ...],
) -> bool:
    intent_by_id = {item.order_intent_id: item for item in intents}
    fill_by_id = {item.fill_id: item for item in fills}
    if len(fill_by_id) != len(fills):
        return False
    fill_events = tuple(
        item
        for item in order_events
        if item.event_type
        in {OrderEventType.PARTIALLY_FILLED, OrderEventType.FILLED}
    )
    if len(fill_events) != len(fills):
        return False
    if {
        item.source_id for item in fill_events
    } != set(fill_by_id):
        return False
    if len(
        {
            (item.order_intent_id, item.quote_id, item.execution_scenario_id)
            for item in fills
        }
    ) != len(fills):
        return False
    event_by_fill_id = {
        item.source_id: item for item in fill_events
    }
    if len(event_by_fill_id) != len(fill_events):
        return False
    for fill in fills:
        intent = intent_by_id.get(fill.order_intent_id)
        event = event_by_fill_id.get(fill.fill_id)
        if intent is None or event is None:
            return False
        if not (
            fill.fill_id
            == stable_id(
                "q1-fill",
                fill.order_intent_id,
                fill.quote_id,
                fill.execution_scenario_id,
            )
            and fill.run_id == intent.run_id
            and fill.arm_id is intent.arm_id
            and fill.symbol == intent.symbol
            and fill.side is intent.side
            and fill.effective_at >= intent.created_at
            and fill.quote_available_at > intent.created_at
            and fill.quote_event_time > intent.created_at
            and event.order_intent_id == fill.order_intent_id
            and event.quantity_delta == fill.quantity
            and event.commission_delta_usd == fill.commission_usd
            and event.cumulative_commission_usd
            == fill.cumulative_order_commission_usd
            and event.quote_id == fill.quote_id
            and event.occurred_at == fill.effective_at
        ):
            return False
    return True


def _risk_episode_hash(item: RiskEpisode) -> str:
    return canonical_hash(
        {
            "run_id": item.run_id,
            "arm_id": item.arm_id,
            "calendar_session_id": item.calendar_session_id,
            "severity": item.severity,
            "triggered_at": item.triggered_at,
            "target_manifest_hash": item.target_manifest_hash,
            "trigger_nav_usd": item.trigger_nav_usd,
            "daily_loss": item.daily_loss,
            "run_drawdown": item.run_drawdown,
            "config_manifest_hash": item.config_manifest_hash,
            "code_version": item.code_version,
            "model_version": item.model_version,
            "source_manifest_hash": item.source_manifest_hash,
        }
    )


def _risk_episode_rows_consistent(
    items: tuple[RiskEpisode, ...],
    rows: tuple[RiskEpisodeRow, ...],
) -> bool:
    if len(items) != len(rows):
        return False
    return all(
        row.risk_episode_id == item.risk_episode_id
        and row.episode_hash == item.episode_hash
        and row.target_manifest_hash == item.target_manifest_hash
        and row.target_count == len(item.targets)
        and row.run_id == item.run_id
        and row.arm_id == item.arm_id.value
        and row.severity == item.severity.value
        and _payload_matches(row.payload_json, item)
        for item, row in zip(items, rows, strict=True)
    )


def _risk_event_hash(item: RiskEpisodeEvent) -> str:
    return canonical_hash(
        {
            "risk_episode_id": item.risk_episode_id,
            "event_type": item.event_type,
            "event_sequence": item.event_sequence,
            "severity": item.severity,
            "target_generation": item.target_generation,
            "targets": item.targets,
            "target_symbol": item.target_symbol,
            "observed_quantity": item.observed_quantity,
            "residual_quantity": item.residual_quantity,
            "occurred_at": item.occurred_at,
            "source_cycle_id": item.source_cycle_id,
            "config_manifest_hash": item.config_manifest_hash,
            "code_version": item.code_version,
            "model_version": item.model_version,
            "source_manifest_hash": item.source_manifest_hash,
        }
    )


def _risk_event_rows_consistent(
    items: tuple[RiskEpisodeEvent, ...],
    rows: tuple[RiskEpisodeEventRow, ...],
) -> bool:
    if len(items) != len(rows):
        return False
    return all(
        row.risk_episode_event_id == item.risk_episode_event_id
        and row.event_hash == item.event_hash
        and row.idempotency_key == item.idempotency_key
        and row.risk_episode_id == item.risk_episode_id
        and row.event_sequence == item.event_sequence
        and row.event_type == item.event_type.value
        and row.severity == item.severity.value
        and row.target_generation == item.target_generation
        and stable_id("q1-risk-event-idem", item.risk_episode_event_id)
        == item.idempotency_key
        and _payload_matches(row.payload_json, item)
        for item, row in zip(items, rows, strict=True)
    )


def _typed_risk_targets_valid(
    episodes: tuple[RiskEpisode, ...],
    *,
    risk_events: tuple[RiskEpisodeEvent, ...],
    risk_event_rows: tuple[RiskEpisodeEventRow, ...],
    target_rows: tuple[RiskEpisodeTargetRow, ...],
) -> bool:
    episodes_by_id = {item.risk_episode_id: item for item in episodes}
    targets_by_episode_generation: dict[
        tuple[str, int],
        dict[str, tuple[RiskTarget, RiskEpisodeTargetRow]],
    ] = {}
    for row in target_rows:
        episode = episodes_by_id.get(row.risk_episode_id)
        if episode is None:
            return False
        try:
            target = RiskTarget.model_validate(row.payload_json)
        except ValueError:
            return False
        target_hash = canonical_hash(target)
        expected_id = target.target_id or stable_id(
            "q1-risk-target",
            row.risk_episode_id,
            target.target_generation,
            target.symbol,
            target_hash,
        )
        if not (
            row.target_hash == target_hash
            and row.risk_target_id == expected_id
            and row.symbol == target.symbol
            and row.target_generation == target.target_generation
            and row.target_quantity == target.target_quantity
            and row.trigger_quantity == target.trigger_quantity
            and row.trigger_price == target.trigger_price
            and row.trigger_quote_id == target.trigger_quote_id
            and row.target_weight == target.target_weight
            and row.config_manifest_hash == episode.config_manifest_hash
            and _payload_matches(row.payload_json, target)
        ):
            return False
        key = (row.risk_episode_id, row.target_generation)
        by_symbol = targets_by_episode_generation.setdefault(key, {})
        if row.symbol in by_symbol:
            return False
        by_symbol[row.symbol] = (target, row)

    event_rows_by_id = {
        row.risk_episode_event_id: row for row in risk_event_rows
    }
    events_by_episode: dict[str, list[RiskEpisodeEvent]] = {}
    for event in risk_events:
        if event.risk_episode_id not in episodes_by_id:
            return False
        events_by_episode.setdefault(event.risk_episode_id, []).append(event)

    referenced_target_ids: set[str] = set()
    for episode in episodes:
        generation_one = targets_by_episode_generation.get(
            (episode.risk_episode_id, 1),
            {},
        )
        stored_activation_targets = tuple(
            pair[0]
            for _, pair in sorted(generation_one.items())
        )
        if not (
            episode.target_manifest_hash == canonical_hash(episode.targets)
            and {
                canonical_hash(item) for item in episode.targets
            }
            == {
                canonical_hash(item) for item in stored_activation_targets
            }
        ):
            return False
        events = sorted(
            events_by_episode.get(episode.risk_episode_id, []),
            key=lambda item: item.event_sequence,
        )
        if not events:
            return False
        previous: RiskEpisodeEvent | None = None
        for expected_sequence, event in enumerate(events, start=1):
            if event.event_sequence != expected_sequence:
                return False
            generation = targets_by_episode_generation.get(
                (episode.risk_episode_id, event.target_generation),
                {},
            )
            if event.event_type in {
                RiskEpisodeEventType.ACTIVATE,
                RiskEpisodeEventType.ESCALATE,
            }:
                if not generation or {
                    canonical_hash(item) for item in event.targets
                } != {
                    canonical_hash(pair[0]) for pair in generation.values()
                }:
                    return False
                referenced_target_ids.update(
                    pair[1].risk_target_id for pair in generation.values()
                )
            row = event_rows_by_id.get(event.risk_episode_event_id)
            if row is None:
                return False
            if event.target_symbol is not None:
                target_pair = generation.get(event.target_symbol)
                if (
                    target_pair is None
                    or row.risk_target_id
                    != target_pair[1].risk_target_id
                ):
                    return False
                referenced_target_ids.add(target_pair[1].risk_target_id)
            elif row.risk_target_id is not None:
                return False
            if previous is None:
                if not (
                    event.event_type is RiskEpisodeEventType.ACTIVATE
                    and event.target_generation == 1
                    and event.severity is episode.severity
                    and tuple(event.targets) == tuple(episode.targets)
                ):
                    return False
            else:
                if previous.event_type is RiskEpisodeEventType.RELEASE:
                    return False
                if event.event_type is RiskEpisodeEventType.ESCALATE:
                    if not (
                        previous.severity is RiskSeverity.HARD_REDUCE
                        and event.severity is RiskSeverity.CRITICAL_EXIT
                        and event.target_generation
                        == previous.target_generation + 1
                    ):
                        return False
                elif event.target_generation != previous.target_generation:
                    return False
                if (
                    event.event_type is not RiskEpisodeEventType.RELEASE
                    and _severity_rank(event.severity)
                    < _severity_rank(previous.severity)
                ):
                    return False
            previous = event
    return referenced_target_ids == {
        row.risk_target_id for row in target_rows
    }


def _settlement_event_hash(item: CashSettlementEvent) -> str:
    return canonical_hash(
        {
            "run_id": item.run_id,
            "arm_id": item.arm_id,
            "event_type": item.event_type,
            "receivable_id": item.receivable_id,
            "source_fill_id": item.source_fill_id,
            "settled_delta": item.settled_cash_delta_usd,
            "unsettled_delta": item.unsettled_receivable_delta_usd,
            "effective_at": item.effective_at,
            "policy_version": item.settlement_policy_version,
            "source_cycle_id": item.source_cycle_id,
            "gross_amount": item.gross_amount_usd,
            "commission": item.commission_usd,
            "trade_at": item.trade_at,
            "settlement_date": (
                None
                if item.settlement_date is None
                else item.settlement_date.isoformat()
            ),
            "calendar_session_id": item.calendar_session_id,
            "config_manifest_hash": item.config_manifest_hash,
            "code_version": item.code_version,
            "model_version": item.model_version,
            "source_manifest_hash": item.source_manifest_hash,
        }
    )


def _settlement_rows_consistent(
    items: tuple[CashSettlementEvent, ...],
    rows: tuple[CashSettlementEventRow, ...],
) -> bool:
    if len(items) != len(rows):
        return False
    return all(
        row.cash_settlement_event_id == item.cash_settlement_event_id
        and row.event_hash == item.event_hash
        and row.idempotency_key == item.idempotency_key
        and row.run_id == item.run_id
        and row.arm_id == item.arm_id.value
        and row.event_type == item.event_type.value
        and row.source_fill_id == item.source_fill_id
        and row.receivable_id == item.receivable_id
        and row.settled_cash_delta_usd
        == item.settled_cash_delta_usd
        and row.unsettled_receivable_delta_usd
        == item.unsettled_receivable_delta_usd
        and stable_id("q1-cash-idem", item.cash_settlement_event_id)
        == item.idempotency_key
        and _payload_matches(row.payload_json, item)
        for item, row in zip(items, rows, strict=True)
    )


def _cash_settlement_economics_valid(
    events: tuple[CashSettlementEvent, ...],
    *,
    fills: tuple[Q1Fill, ...],
) -> bool:
    fill_by_id = {item.fill_id: item for item in fills}
    trade_events_by_fill: dict[str, list[CashSettlementEvent]] = {}
    receivable_created: dict[str, CashSettlementEvent] = {}
    openings_by_arm: dict[str, int] = {}
    for event in events:
        if (
            stable_id(
                "q1-cash-event",
                {
                    "run_id": event.run_id,
                    "arm_id": event.arm_id,
                    "event_type": event.event_type,
                    "receivable_id": event.receivable_id,
                    "source_fill_id": event.source_fill_id,
                    "settled_delta": event.settled_cash_delta_usd,
                    "unsettled_delta": (
                        event.unsettled_receivable_delta_usd
                    ),
                    "effective_at": event.effective_at,
                    "policy_version": event.settlement_policy_version,
                    "source_cycle_id": event.source_cycle_id,
                },
            )
            != event.cash_settlement_event_id
        ):
            return False
        if event.event_type is CashSettlementEventType.OPENING_SETTLED_CASH:
            openings_by_arm[event.arm_id.value] = (
                openings_by_arm.get(event.arm_id.value, 0) + 1
            )
            if not (
                event.source_fill_id is None
                and event.receivable_id is None
                and event.settled_cash_delta_usd
                == event.gross_amount_usd
                and event.unsettled_receivable_delta_usd == 0
                and event.commission_usd == 0
                and event.trade_at is None
                and event.settlement_date is None
            ):
                return False
            continue
        if event.event_type in {
            CashSettlementEventType.BUY_SETTLED_CASH_DEBIT,
            CashSettlementEventType.SELL_RECEIVABLE_CREATED,
        }:
            if event.source_fill_id is None:
                return False
            trade_events_by_fill.setdefault(event.source_fill_id, []).append(
                event
            )
        if event.event_type is CashSettlementEventType.SELL_RECEIVABLE_CREATED:
            if (
                event.receivable_id is None
                or event.receivable_id in receivable_created
            ):
                return False
            receivable_created[event.receivable_id] = event

    if set(trade_events_by_fill) != set(fill_by_id):
        return False
    for fill_id, fill in fill_by_id.items():
        matches = trade_events_by_fill.get(fill_id, [])
        if len(matches) != 1:
            return False
        event = matches[0]
        notional = fill.quantity * fill.price
        common_valid = (
            event.run_id == fill.run_id
            and event.arm_id is fill.arm_id
            and event.source_fill_id == fill.fill_id
            and event.gross_amount_usd == notional
            and event.commission_usd == fill.commission_usd
            and event.trade_at == fill.effective_at
            and event.effective_at == fill.effective_at
        )
        if fill.side is OrderSide.BUY:
            if not (
                common_valid
                and event.event_type
                is CashSettlementEventType.BUY_SETTLED_CASH_DEBIT
                and event.receivable_id is None
                and event.settled_cash_delta_usd
                == -(notional + fill.commission_usd)
                and event.unsettled_receivable_delta_usd == 0
                and event.settlement_date is None
            ):
                return False
        elif not (
            common_valid
            and event.event_type
            is CashSettlementEventType.SELL_RECEIVABLE_CREATED
            and event.receivable_id is not None
            and event.settled_cash_delta_usd == 0
            and event.unsettled_receivable_delta_usd
            == notional - fill.commission_usd
            and event.settlement_date is not None
        ):
            return False

    for event in events:
        if event.event_type is not CashSettlementEventType.RECEIVABLE_SETTLED:
            continue
        if event.receivable_id is None:
            return False
        source = receivable_created.get(event.receivable_id)
        if source is None or not (
            event.source_fill_id == source.source_fill_id
            and event.gross_amount_usd == source.gross_amount_usd
            and event.commission_usd == source.commission_usd
            and event.settlement_date == source.settlement_date
            and event.settled_cash_delta_usd
            == source.unsettled_receivable_delta_usd
            and event.unsettled_receivable_delta_usd
            == -source.unsettled_receivable_delta_usd
        ):
            return False
    try:
        for arm_id in {item.arm_id for item in events}:
            arm_events = tuple(
                item for item in events if item.arm_id is arm_id
            )
            apply_settlement_events(
                events=arm_events,
                as_of=max(item.created_at for item in arm_events),
            )
    except (ValueError, InvalidOperation):
        return False
    return all(count == 1 for count in openings_by_arm.values())


def _state_hashes(
    rows: tuple[ArmStateSnapshotRow, ...],
    *,
    fills: tuple[Q1Fill, ...],
    settlement_events: tuple[CashSettlementEvent, ...],
) -> tuple[
    tuple[str, ...],
    bool,
    dict[tuple[str, int], Q1ArmState],
]:
    hashes: list[str] = []
    expected_sequences: dict[str, int] = {}
    valid = True
    states_by_sequence: dict[tuple[str, int], Q1ArmState] = {}
    for row in rows:
        try:
            state = Q1ArmState.from_payload(row.payload_json)
        except (ValueError, KeyError, InvalidOperation):
            return tuple(hashes), False, states_by_sequence
        replayed_hash = canonical_hash(state.as_payload())
        hashes.append(replayed_hash)
        expected = expected_sequences.get(row.arm_id, 0)
        expected_row_id = stable_id(
            "q1-arm-state",
            row.run_id,
            row.arm_id,
            row.sequence,
            row.source_cycle_id,
            replayed_hash,
        )
        valid = (
            valid
            and state.arm_id == row.arm_id
            and state.sequence == row.sequence == expected
            and replayed_hash == row.state_hash
            and expected_row_id == row.arm_state_snapshot_id
        )
        states_by_sequence[(row.arm_id, row.sequence)] = state
        expected_sequences[row.arm_id] = expected + 1
    transitions = _state_transitions(
        fills,
        settlement_events=settlement_events,
    )
    used_transition_ids: set[str] = set()
    rows_by_arm: dict[str, list[ArmStateSnapshotRow]] = {}
    for row in rows:
        rows_by_arm.setdefault(row.arm_id, []).append(row)
    for arm_id, arm_rows in rows_by_arm.items():
        ordered_rows = sorted(arm_rows, key=lambda item: item.sequence)
        if not ordered_rows or ordered_rows[0].sequence != 0:
            valid = False
            continue
        current = states_by_sequence[(arm_id, 0)]
        for row in ordered_rows[1:]:
            target = states_by_sequence[(arm_id, row.sequence)]
            candidates: list[tuple[_StateTransition, Q1ArmState]] = []
            for transition in transitions:
                if (
                    transition.transition_id in used_transition_ids
                    or transition.arm_id != arm_id
                    or transition.source_cycle_id != row.source_cycle_id
                    or _aware(transition.created_at)
                    != _aware(row.created_at)
                ):
                    continue
                replayed = _apply_state_transition(
                    current,
                    transition,
                    settlement_events=settlement_events,
                )
                if (
                    replayed is not None
                    and replayed.as_payload() == target.as_payload()
                ):
                    candidates.append((transition, replayed))
            if len(candidates) != 1:
                valid = False
                current = target
                continue
            transition, current = candidates[0]
            used_transition_ids.add(transition.transition_id)
    valid = valid and used_transition_ids == {
        item.transition_id for item in transitions
    }
    return tuple(hashes), valid, states_by_sequence


def _state_transitions(
    fills: tuple[Q1Fill, ...],
    *,
    settlement_events: tuple[CashSettlementEvent, ...],
) -> tuple[_StateTransition, ...]:
    transitions = [
        _StateTransition(
            transition_id=f"FILL:{item.fill_id}",
            arm_id=item.arm_id.value,
            source_cycle_id=item.source_cycle_id,
            created_at=item.created_at,
            fill=item,
        )
        for item in fills
    ]
    transitions.extend(
        _StateTransition(
            transition_id=(
                f"SETTLEMENT:{item.cash_settlement_event_id}"
            ),
            arm_id=item.arm_id.value,
            source_cycle_id=item.source_cycle_id,
            created_at=item.created_at,
            settlement=item,
        )
        for item in settlement_events
        if item.event_type is CashSettlementEventType.RECEIVABLE_SETTLED
    )
    return tuple(transitions)


def _apply_state_transition(
    state: Q1ArmState,
    transition: _StateTransition,
    *,
    settlement_events: tuple[CashSettlementEvent, ...],
) -> Q1ArmState | None:
    try:
        if transition.fill is not None:
            fill = transition.fill
            cash_events = [
                item
                for item in settlement_events
                if (
                    item.source_fill_id == fill.fill_id
                    and item.event_type
                    in {
                        CashSettlementEventType.BUY_SETTLED_CASH_DEBIT,
                        CashSettlementEventType.SELL_RECEIVABLE_CREATED,
                    }
                )
            ]
            if len(cash_events) != 1:
                return None
            cash_event = cash_events[0]
            receivable = None
            if fill.side is OrderSide.SELL:
                if (
                    cash_event.receivable_id is None
                    or cash_event.settlement_date is None
                ):
                    return None
                receivable = UnsettledReceivable(
                    receivable_id=cash_event.receivable_id,
                    source_fill_id=fill.fill_id,
                    amount_usd=cash_event.unsettled_receivable_delta_usd,
                    settlement_date=cash_event.settlement_date,
                    created_at=cash_event.created_at,
                )
            return state.apply_fill(
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
                ),
                sell_receivable=receivable,
            )
        settlement = transition.settlement
        if settlement is None or settlement.receivable_id is None:
            return None
        replayed = state.settle(settlement.receivable_id)
        return None if replayed is state else replayed
    except (ValueError, KeyError, InvalidOperation):
        return None


def _nav_hashes(
    rows: tuple[NavSnapshotRow, ...],
    *,
    state_rows: tuple[ArmStateSnapshotRow, ...],
    states_by_sequence: dict[tuple[str, int], Q1ArmState],
) -> tuple[tuple[str, ...], bool]:
    hashes: list[str] = []
    valid = True
    for row in rows:
        payload = dict(row.payload_json)
        stored_hash = payload.pop("nav_hash", None)
        replayed_hash = canonical_hash(payload)
        hashes.append(replayed_hash)
        state = _state_as_of(
            arm_id=row.arm_id,
            as_of=row.as_of,
            state_rows=state_rows,
            states_by_sequence=states_by_sequence,
        )
        valid = (
            valid
            and stored_hash == replayed_hash
            and row.nav_snapshot_id
            == stable_id(
                "q1-nav",
                row.run_id,
                row.arm_id,
                row.source_cycle_id,
                _aware(row.as_of),
                replayed_hash,
            )
            and state is not None
            and _nav_payload_matches_state(
                row,
                payload=payload,
                state=state,
            )
        )
    return tuple(hashes), valid


def _state_as_of(
    *,
    arm_id: str,
    as_of: datetime,
    state_rows: tuple[ArmStateSnapshotRow, ...],
    states_by_sequence: dict[tuple[str, int], Q1ArmState],
) -> Q1ArmState | None:
    eligible = [
        row
        for row in state_rows
        if row.arm_id == arm_id and _aware(row.created_at) <= _aware(as_of)
    ]
    if not eligible:
        return None
    latest = max(
        eligible,
        key=lambda item: (
            item.sequence,
            _aware(item.created_at),
            item.arm_state_snapshot_id,
        ),
    )
    return states_by_sequence.get((arm_id, latest.sequence))


def _nav_payload_matches_state(
    row: NavSnapshotRow,
    *,
    payload: dict[str, Any],
    state: Q1ArmState,
) -> bool:
    try:
        if payload.get("schema_version") != "q1_nav_v1":
            return False
        settled = Decimal(str(payload["settled_cash_usd"]))
        unsettled = Decimal(str(payload["unsettled_receivables_usd"]))
        positions_value = Decimal(
            str(payload["positions_market_value_usd"])
        )
        raw_weights = payload["actual_weights"]
        if not isinstance(raw_weights, dict):
            return False
        typed_weights = cast(
            "dict[str, str | int | float | Decimal]",
            raw_weights,
        )
        weights = {
            str(symbol): Decimal(str(weight))
            for symbol, weight in typed_weights.items()
        }
        nav = Decimal(row.nav_usd)
        if "nav_usd" in payload and Decimal(str(payload["nav_usd"])) != nav:
            return False
    except (KeyError, InvalidOperation, TypeError):
        return False
    tolerance = max(
        Decimal("0.00000001"),
        abs(nav) * Decimal("0.0000000001"),
    )
    non_cash = {
        symbol: weight
        for symbol, weight in weights.items()
        if symbol != "USD_CASH"
    }
    expected_symbols = {
        symbol
        for symbol, quantity in state.positions.items()
        if quantity > 0
    }
    return (
        nav > 0
        and payload.get("real_order_routing") is False
        and settled == state.settled_cash_usd
        and unsettled == state.unsettled_cash_usd
        and abs(
            nav - settled - unsettled - positions_value
        )
        <= tolerance
        and set(non_cash) == expected_symbols
        and all(weight >= 0 for weight in weights.values())
        and abs(sum(weights.values(), Decimal("0")) - Decimal("1"))
        <= Decimal("0.0000000001")
        and abs(
            weights.get("USD_CASH", Decimal("-1"))
            - state.total_cash_usd / nav
        )
        <= Decimal("0.0000000001")
        and abs(
            sum(non_cash.values(), Decimal("0")) * nav
            - positions_value
        )
        <= tolerance
    )


def _daily_results_valid(
    items: tuple[StrategyDailyResult, ...],
    rows: tuple[StrategyDailyResultRow, ...],
) -> bool:
    if len(items) != len(rows):
        return False
    return all(
        canonical_hash(
            item.model_dump(
                exclude={"strategy_daily_result_id", "result_hash"}
            )
        )
        == item.result_hash
        and stable_id(
            "q1-daily-result",
            item.run_id,
            item.arm_id,
            item.session_date,
            item.result_hash,
        )
        == item.strategy_daily_result_id
        and row.strategy_daily_result_id
        == item.strategy_daily_result_id
        and row.result_hash == item.result_hash
        and row.nav_usd == item.nav_usd
        and row.net_daily_return == item.net_daily_return
        and row.cumulative_return == item.cumulative_return
        and _payload_matches(row.payload_json, item)
        for item, row in zip(items, rows, strict=True)
    )


def _matched_results_valid(
    items: tuple[MatchedAttributionResult, ...],
    rows: tuple[MatchedAttributionResultRow, ...],
    *,
    daily_results: tuple[StrategyDailyResult, ...],
) -> bool:
    if len(items) != len(rows):
        return False
    daily_by_arm_date = {
        (item.arm_id, item.session_date): item.net_daily_return
        for item in daily_results
    }
    for item, row in zip(items, rows, strict=True):
        left_dates = {
            date
            for arm, date in daily_by_arm_date
            if arm == item.left_arm_id and date <= item.through_session_date
        }
        right_dates = {
            date
            for arm, date in daily_by_arm_date
            if arm == item.right_arm_id and date <= item.through_session_date
        }
        common_dates = sorted(left_dates & right_dates)
        mean_difference = Decimal("0")
        if common_dates:
            mean_difference = sum(
                (
                    daily_by_arm_date[(item.left_arm_id, session_date)]
                    - daily_by_arm_date[(item.right_arm_id, session_date)]
                    for session_date in common_dates
                ),
                Decimal("0"),
            ) / Decimal(len(common_dates))
        if not (
            canonical_hash(
                item.model_dump(
                    exclude={
                        "matched_attribution_result_id",
                        "result_hash",
                    }
                )
            )
            == item.result_hash
            and stable_id(
                "q1-matched-result",
                item.run_id,
                item.comparison,
                item.through_session_date,
                item.result_hash,
            )
            == item.matched_attribution_result_id
            and row.matched_attribution_result_id
            == item.matched_attribution_result_id
            and row.result_hash == item.result_hash
            and row.common_valid_sessions == item.common_valid_sessions
            and item.common_valid_sessions == len(common_dates)
            and abs(item.mean_daily_difference - mean_difference)
            <= Decimal("0.00000000000001")
            and abs(
                item.annualized_difference
                - mean_difference * Decimal("252")
            )
            <= Decimal("0.00000000000001")
            and _payload_matches(row.payload_json, item)
        ):
            return False
    return True


def _payload_matches(
    payload: dict[str, Any],
    item: Any,
) -> bool:
    return canonical_hash(payload) == canonical_hash(model_payload(item))


def _severity_rank(value: RiskSeverity) -> int:
    return {
        RiskSeverity.NORMAL: 0,
        RiskSeverity.SOFT_STOP: 1,
        RiskSeverity.HARD_REDUCE: 2,
        RiskSeverity.CRITICAL_EXIT: 3,
    }[value]


def _aware(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=UTC)
        if value.tzinfo is None
        else value.astimezone(UTC)
    )


def _config_hashes_consistent(
    expected: str,
    records: tuple[Any, ...],
) -> bool:
    return all(
        getattr(item, "config_manifest_hash", None) == expected
        for item in records
    )
