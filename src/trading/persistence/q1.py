from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from trading.domain.contracts import model_payload
from trading.domain.hashing import canonical_hash, stable_id
from trading.domain.q1 import (
    TERMINAL_ORDER_EVENT_TYPES,
    CashSettlementEvent,
    MarketCalendarSession,
    MatchedAttributionResult,
    OrderEvent,
    OrderEventType,
    Q1StrategyDecision,
    RiskEpisode,
    RiskEpisodeEvent,
    RiskEpisodeEventType,
    RiskSeverity,
    RiskTarget,
    StrategyDailyResult,
    StrategyEvaluationAnchor,
)
from trading.persistence.models import (
    CashSettlementEventRow,
    MarketCalendarSessionRow,
    MatchedAttributionResultRow,
    OrderEventRow,
    OrderIntentRow,
    PaperCycleRow,
    PortfolioDecisionRow,
    RiskEpisodeEventRow,
    RiskEpisodeRow,
    RiskEpisodeTargetRow,
    StrategyDailyResultRow,
    StrategyEvaluationAnchorRow,
)


class Q1PersistenceConflict(RuntimeError):
    pass


class Q1CycleFenceError(Q1PersistenceConflict):
    pass


@dataclass(frozen=True, slots=True)
class PendingOrderProjection:
    order: OrderIntentRow
    latest_event: OrderEventRow


@dataclass(frozen=True, slots=True)
class ActiveRiskEpisodeProjection:
    episode: RiskEpisodeRow
    latest_event: RiskEpisodeEventRow
    targets: tuple[RiskEpisodeTargetRow, ...]


@dataclass(frozen=True, slots=True)
class CashBalanceProjection:
    settled_cash_usd: Decimal
    unsettled_receivables_usd: Decimal

    @property
    def total_cash_usd(self) -> Decimal:
        return self.settled_cash_usd + self.unsettled_receivables_usd


class MarketCalendarSessionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def append(self, item: MarketCalendarSession) -> MarketCalendarSessionRow:
        existing_by_id = self._session.get(
            MarketCalendarSessionRow,
            item.calendar_session_id,
        )
        if existing_by_id is not None:
            if (
                existing_by_id.algorithm_version != item.algorithm_version
                or existing_by_id.calendar_version != item.calendar_version
                or existing_by_id.session_date != item.session_date
                or existing_by_id.open_at != item.open_at
                or existing_by_id.close_at != item.close_at
                or existing_by_id.source != item.source
            ):
                raise Q1PersistenceConflict(
                    "Calendar session ID has different immutable market hours"
                )
            # A repeated provider fetch can observe the same source payload at
            # a later available_at and under a newer process provenance. The
            # stable calendar ID denotes that source payload, so the first
            # append remains authoritative.
            return existing_by_id
        existing = self._session.scalar(
            select(MarketCalendarSessionRow).where(
                MarketCalendarSessionRow.calendar_version == item.calendar_version,
                MarketCalendarSessionRow.session_date == item.session_date,
                MarketCalendarSessionRow.session_hash == item.session_hash,
            )
        )
        if existing is not None:
            if (
                existing.calendar_session_id != item.calendar_session_id
                or existing.payload_json != model_payload(item)
            ):
                raise Q1PersistenceConflict(
                    "Calendar revision identity has different immutable content"
                )
            return existing
        row = MarketCalendarSessionRow(
            calendar_session_id=item.calendar_session_id,
            algorithm_version=item.algorithm_version,
            calendar_version=item.calendar_version,
            session_date=item.session_date,
            open_at=item.open_at,
            close_at=item.close_at,
            source=item.source,
            available_at=item.available_at,
            config_manifest_hash=item.config_manifest_hash,
            code_version=item.code_version,
            model_version=item.model_version,
            source_manifest_hash=item.source_manifest_hash,
            session_hash=item.session_hash,
            payload_json=model_payload(item),
            created_at=item.created_at,
        )
        self._session.add(row)
        return row

    def for_date_as_of(
        self,
        *,
        calendar_version: str,
        session_date: date,
        cutoff: datetime,
    ) -> MarketCalendarSessionRow | None:
        return self._session.scalar(
            select(MarketCalendarSessionRow)
            .where(
                MarketCalendarSessionRow.calendar_version == calendar_version,
                MarketCalendarSessionRow.session_date == session_date,
                MarketCalendarSessionRow.available_at <= cutoff,
            )
            .order_by(
                MarketCalendarSessionRow.available_at.desc(),
                MarketCalendarSessionRow.calendar_session_id.desc(),
            )
            .limit(1)
        )

    def get(self, calendar_session_id: str) -> MarketCalendarSessionRow | None:
        return self._session.get(MarketCalendarSessionRow, calendar_session_id)


class StrategyEvaluationAnchorRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def append(self, item: StrategyEvaluationAnchor) -> StrategyEvaluationAnchorRow:
        existing = self.for_run(item.run_id)
        if existing is not None:
            if existing.anchor_hash != item.anchor_hash:
                raise Q1PersistenceConflict(
                    f"Run {item.run_id!r} already has a different evaluation anchor"
                )
            return existing
        row = StrategyEvaluationAnchorRow(
            evaluation_anchor_id=item.evaluation_anchor_id,
            run_id=item.run_id,
            algorithm_version=item.algorithm_version,
            calendar_session_id=item.calendar_session_id,
            common_t0_at=item.common_t0_at,
            initial_nav_usd=item.initial_nav_usd,
            quote_manifest_hash=item.quote_manifest_hash,
            config_manifest_hash=item.config_manifest_hash,
            code_version=item.code_version,
            model_version=item.model_version,
            source_manifest_hash=item.source_manifest_hash,
            anchor_hash=item.anchor_hash,
            payload_json=model_payload(item),
            created_at=item.created_at,
        )
        self._session.add(row)
        return row

    def for_run(self, run_id: str) -> StrategyEvaluationAnchorRow | None:
        return self._session.scalar(
            select(StrategyEvaluationAnchorRow).where(
                StrategyEvaluationAnchorRow.run_id == run_id
            )
        )


class Q1StrategyDecisionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def append(self, item: Q1StrategyDecision) -> PortfolioDecisionRow:
        existing = self._session.get(
            PortfolioDecisionRow,
            item.portfolio_decision_id,
        )
        if existing is not None:
            if existing.decision_hash != item.decision_hash:
                raise Q1PersistenceConflict(
                    "Strategy decision ID has different immutable content"
                )
            return existing
        _require_cycle_fence(
            self._session,
            cycle_id=item.source_cycle_id,
            lease_owner=item.worker_fence_token,
            attempt_count=item.cycle_attempt_count,
            fallback_now=item.decision_created_at,
        )
        row = PortfolioDecisionRow(
            portfolio_decision_id=item.portfolio_decision_id,
            run_id=item.run_id,
            arm_id=item.arm_id.value,
            source_cycle_id=item.source_cycle_id,
            input_state_sequence=item.input_state_sequence,
            decision_time=item.scheduled_at,
            algorithm_version=item.algorithm_version,
            scheduled_at=item.scheduled_at,
            signal_data_cutoff=item.signal_data_cutoff,
            portfolio_state_as_of=item.portfolio_state_as_of,
            quote_as_of=item.quote_as_of,
            decision_created_at=item.decision_created_at,
            valid_until=item.valid_until,
            calendar_session_id=item.input_manifest.calendar_session_id,
            config_manifest_hash=item.config_manifest_hash,
            code_version=item.code_version,
            model_version=item.model_version,
            source_manifest_hash=item.source_manifest_hash,
            input_manifest_hash=item.input_manifest.manifest_hash,
            payload_json=model_payload(item),
            decision_hash=item.decision_hash,
        )
        self._session.add(row)
        return row

    def latest_as_of(
        self,
        *,
        run_id: str,
        arm_id: str,
        as_of: datetime,
    ) -> PortfolioDecisionRow | None:
        return self._session.scalar(
            select(PortfolioDecisionRow)
            .where(
                PortfolioDecisionRow.run_id == run_id,
                PortfolioDecisionRow.arm_id == arm_id,
                PortfolioDecisionRow.algorithm_version == "q1_math_core_v1",
                PortfolioDecisionRow.decision_created_at <= as_of,
            )
            .order_by(
                PortfolioDecisionRow.decision_created_at.desc(),
                PortfolioDecisionRow.portfolio_decision_id.desc(),
            )
            .limit(1)
        )


class OrderEventRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def append(self, event: OrderEvent) -> OrderEventRow:
        existing = self._session.scalar(
            select(OrderEventRow).where(
                OrderEventRow.idempotency_key == event.idempotency_key
            )
        )
        if existing is not None:
            if existing.event_hash != event.event_hash:
                raise Q1PersistenceConflict(
                    "Order-event idempotency key has different immutable content"
                )
            return existing
        if event.source_cycle_id is None:
            raise Q1CycleFenceError("Q1 order events require a source cycle")
        _require_cycle_fence(
            self._session,
            cycle_id=event.source_cycle_id,
            lease_owner=event.worker_fence_token,
            attempt_count=event.cycle_attempt_count,
            fallback_now=event.available_at,
        )
        intent = self._session.get(OrderIntentRow, event.order_intent_id)
        if intent is None:
            raise Q1PersistenceConflict(
                f"Unknown order intent {event.order_intent_id!r}"
            )
        latest = self.latest_for_order(event.order_intent_id)
        self._validate_transition(event=event, intent=intent, latest=latest)
        row = OrderEventRow(
            order_event_id=event.event_id,
            order_intent_id=event.order_intent_id,
            run_id=intent.run_id,
            arm_id=intent.arm_id,
            source_cycle_id=event.source_cycle_id,
            algorithm_version=event.algorithm_version,
            event_sequence=event.event_sequence,
            event_type=event.event_type.value,
            quantity_delta=event.quantity_delta,
            commission_delta_usd=event.commission_delta_usd,
            remaining_quantity=event.remaining_quantity,
            cumulative_filled_quantity=event.cumulative_filled_quantity,
            cumulative_commission_usd=event.cumulative_commission_usd,
            quote_id=event.quote_id,
            occurred_at=event.occurred_at,
            available_at=event.available_at,
            reason=event.reason,
            source_id=event.source_id,
            worker_fence_token=event.worker_fence_token,
            cycle_attempt_count=event.cycle_attempt_count,
            idempotency_key=event.idempotency_key,
            config_manifest_hash=event.config_manifest_hash,
            code_version=event.code_version,
            model_version=event.model_version,
            source_manifest_hash=event.source_manifest_hash,
            event_hash=event.event_hash,
            payload_json=model_payload(event),
            created_at=event.available_at,
        )
        self._session.add(row)
        return row

    def latest_for_order(
        self,
        order_intent_id: str,
        *,
        as_of: datetime | None = None,
    ) -> OrderEventRow | None:
        statement = select(OrderEventRow).where(
            OrderEventRow.order_intent_id == order_intent_id
        )
        if as_of is not None:
            statement = statement.where(OrderEventRow.available_at <= as_of)
        return self._session.scalar(
            statement.order_by(
                OrderEventRow.event_sequence.desc(),
                OrderEventRow.order_event_id.desc(),
            ).limit(1)
        )

    def pending(
        self,
        *,
        run_id: str,
        arm_id: str | None = None,
        as_of: datetime | None = None,
    ) -> list[PendingOrderProjection]:
        latest = (
            select(
                OrderEventRow.order_intent_id.label("order_intent_id"),
                func.max(OrderEventRow.event_sequence).label("event_sequence"),
            )
            .where(OrderEventRow.run_id == run_id)
        )
        if arm_id is not None:
            latest = latest.where(OrderEventRow.arm_id == arm_id)
        if as_of is not None:
            latest = latest.where(OrderEventRow.available_at <= as_of)
        latest_subquery = latest.group_by(OrderEventRow.order_intent_id).subquery()
        statement = (
            select(OrderIntentRow, OrderEventRow)
            .join(
                latest_subquery,
                latest_subquery.c.order_intent_id
                == OrderIntentRow.order_intent_id,
            )
            .join(
                OrderEventRow,
                (OrderEventRow.order_intent_id == latest_subquery.c.order_intent_id)
                & (
                    OrderEventRow.event_sequence
                    == latest_subquery.c.event_sequence
                ),
            )
            .where(
                OrderEventRow.event_type.not_in(
                    [item.value for item in TERMINAL_ORDER_EVENT_TYPES]
                ),
                OrderEventRow.remaining_quantity > 0,
            )
            .order_by(OrderIntentRow.created_at, OrderIntentRow.order_intent_id)
        )
        return [
            PendingOrderProjection(order=order, latest_event=event)
            for order, event in self._session.execute(statement).all()
        ]

    @staticmethod
    def _validate_transition(
        *,
        event: OrderEvent,
        intent: OrderIntentRow,
        latest: OrderEventRow | None,
    ) -> None:
        if intent.quantity is None:
            raise Q1PersistenceConflict("Q1 order intent requires typed quantity")
        if latest is None:
            if event.event_sequence != 1 or event.event_type is not OrderEventType.CREATED:
                raise Q1PersistenceConflict("First order event must be CREATED sequence 1")
            if (
                event.quantity_delta != 0
                or event.cumulative_filled_quantity != 0
                or event.cumulative_commission_usd != 0
                or event.remaining_quantity != intent.quantity
            ):
                raise Q1PersistenceConflict("CREATED event quantities do not match intent")
            return
        if OrderEventType(latest.event_type) in TERMINAL_ORDER_EVENT_TYPES:
            raise Q1PersistenceConflict("Terminal order cannot accept another event")
        if event.event_sequence != latest.event_sequence + 1:
            raise Q1PersistenceConflict("Order event sequence is not contiguous")
        is_fill = event.event_type in {
            OrderEventType.PARTIALLY_FILLED,
            OrderEventType.FILLED,
        }
        expected_fill = latest.cumulative_filled_quantity
        expected_remaining = latest.remaining_quantity
        if is_fill:
            expected_fill += event.quantity_delta
            expected_remaining -= event.quantity_delta
        if event.cumulative_filled_quantity != expected_fill:
            raise Q1PersistenceConflict("Order cumulative fill is inconsistent")
        if event.remaining_quantity != expected_remaining:
            raise Q1PersistenceConflict("Order remaining quantity is inconsistent")
        if (
            event.cumulative_commission_usd
            != latest.cumulative_commission_usd + event.commission_delta_usd
        ):
            raise Q1PersistenceConflict("Order cumulative commission is inconsistent")


class RiskEpisodeRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def append_episode(
        self,
        episode: RiskEpisode,
        activation_event: RiskEpisodeEvent,
    ) -> RiskEpisodeRow:
        if (
            activation_event.risk_episode_id != episode.risk_episode_id
            or activation_event.event_type is not RiskEpisodeEventType.ACTIVATE
            or activation_event.severity is not episode.severity
            or tuple(activation_event.targets) != tuple(episode.targets)
        ):
            raise Q1PersistenceConflict(
                "Risk activation event must exactly match its typed episode"
            )
        existing = self._session.get(RiskEpisodeRow, episode.risk_episode_id)
        if existing is not None:
            if existing.episode_hash != episode.episode_hash:
                raise Q1PersistenceConflict(
                    "Risk episode ID has different immutable content"
                )
            return existing
        if self.active(run_id=episode.run_id, arm_id=episode.arm_id.value) is not None:
            raise Q1PersistenceConflict(
                "An active typed risk episode must be escalated, not replaced"
            )
        row = RiskEpisodeRow(
            risk_episode_id=episode.risk_episode_id,
            run_id=episode.run_id,
            arm_id=episode.arm_id.value,
            algorithm_version=episode.algorithm_version,
            severity=episode.severity.value,
            calendar_session_id=episode.calendar_session_id,
            triggered_at=episode.triggered_at,
            trigger_nav_usd=episode.trigger_nav_usd,
            session_open_nav_usd=episode.session_open_nav_usd,
            running_peak_nav_usd=episode.running_peak_nav_usd,
            daily_loss=episode.daily_loss,
            run_drawdown=episode.run_drawdown,
            portfolio_annualized_vol=episode.portfolio_annualized_vol,
            soft_daily_threshold=episode.soft_daily_threshold,
            hard_daily_threshold=episode.hard_daily_threshold,
            reconciliation_status=episode.reconciliation_status,
            target_manifest_hash=episode.target_manifest_hash,
            target_count=len(episode.targets),
            config_manifest_hash=episode.config_manifest_hash,
            code_version=episode.code_version,
            model_version=episode.model_version,
            source_manifest_hash=episode.source_manifest_hash,
            episode_hash=episode.episode_hash,
            payload_json=model_payload(episode),
            created_at=episode.created_at,
        )
        self._session.add(row)
        self._append_targets(
            episode_id=episode.risk_episode_id,
            targets=episode.targets,
            config_manifest_hash=episode.config_manifest_hash,
            created_at=episode.created_at,
        )
        self._session.flush()
        self.append_event(activation_event)
        return row

    def append_event(self, event: RiskEpisodeEvent) -> RiskEpisodeEventRow:
        existing = self._session.scalar(
            select(RiskEpisodeEventRow).where(
                RiskEpisodeEventRow.idempotency_key == event.idempotency_key
            )
        )
        if existing is not None:
            if existing.event_hash != event.event_hash:
                raise Q1PersistenceConflict(
                    "Risk-event idempotency key has different immutable content"
                )
            return existing
        if event.source_cycle_id is None:
            raise Q1CycleFenceError("Q1 risk events require a source cycle")
        _require_cycle_fence(
            self._session,
            cycle_id=event.source_cycle_id,
            lease_owner=event.worker_fence_token,
            attempt_count=event.cycle_attempt_count,
            fallback_now=event.available_at,
        )
        episode = self._session.get(RiskEpisodeRow, event.risk_episode_id)
        if episode is None:
            raise Q1PersistenceConflict(
                f"Unknown risk episode {event.risk_episode_id!r}"
            )
        latest = self.latest_event(event.risk_episode_id)
        self._validate_transition(event=event, latest=latest)
        if event.event_type is RiskEpisodeEventType.ESCALATE:
            self._append_targets(
                episode_id=event.risk_episode_id,
                targets=event.targets,
                config_manifest_hash=event.config_manifest_hash,
                created_at=event.available_at,
            )
            self._session.flush()
        target = None
        if event.target_symbol is not None:
            target = self._session.scalar(
                select(RiskEpisodeTargetRow).where(
                    RiskEpisodeTargetRow.risk_episode_id == event.risk_episode_id,
                    RiskEpisodeTargetRow.symbol == event.target_symbol,
                    RiskEpisodeTargetRow.target_generation
                    == event.target_generation,
                )
            )
            if target is None:
                raise Q1PersistenceConflict(
                    "Risk progress event references an unknown typed target"
                )
        row = RiskEpisodeEventRow(
            risk_episode_event_id=event.risk_episode_event_id,
            risk_episode_id=event.risk_episode_id,
            run_id=episode.run_id,
            arm_id=episode.arm_id,
            source_cycle_id=event.source_cycle_id,
            risk_target_id=None if target is None else target.risk_target_id,
            algorithm_version=event.algorithm_version,
            event_sequence=event.event_sequence,
            event_type=event.event_type.value,
            severity=event.severity.value,
            target_generation=event.target_generation,
            observed_quantity=event.observed_quantity,
            residual_quantity=event.residual_quantity,
            consecutive_valid_checks=event.consecutive_valid_checks,
            occurred_at=event.occurred_at,
            available_at=event.available_at,
            worker_fence_token=event.worker_fence_token,
            cycle_attempt_count=event.cycle_attempt_count,
            idempotency_key=event.idempotency_key,
            config_manifest_hash=event.config_manifest_hash,
            code_version=event.code_version,
            model_version=event.model_version,
            source_manifest_hash=event.source_manifest_hash,
            event_hash=event.event_hash,
            payload_json=model_payload(event),
            created_at=event.available_at,
        )
        self._session.add(row)
        return row

    def latest_event(self, risk_episode_id: str) -> RiskEpisodeEventRow | None:
        return self._session.scalar(
            select(RiskEpisodeEventRow)
            .where(RiskEpisodeEventRow.risk_episode_id == risk_episode_id)
            .order_by(
                RiskEpisodeEventRow.event_sequence.desc(),
                RiskEpisodeEventRow.risk_episode_event_id.desc(),
            )
            .limit(1)
        )

    def active(
        self,
        *,
        run_id: str,
        arm_id: str,
    ) -> ActiveRiskEpisodeProjection | None:
        episodes = list(
            self._session.scalars(
                select(RiskEpisodeRow)
                .where(
                    RiskEpisodeRow.run_id == run_id,
                    RiskEpisodeRow.arm_id == arm_id,
                )
                .order_by(
                    RiskEpisodeRow.triggered_at.desc(),
                    RiskEpisodeRow.risk_episode_id.desc(),
                )
            )
        )
        active: list[ActiveRiskEpisodeProjection] = []
        severity_rank = {
            RiskSeverity.HARD_REDUCE.value: 1,
            RiskSeverity.CRITICAL_EXIT.value: 2,
        }
        for episode in episodes:
            latest = self.latest_event(episode.risk_episode_id)
            if latest is None or latest.event_type == RiskEpisodeEventType.RELEASE.value:
                continue
            targets = self.targets(
                episode.risk_episode_id,
                generation=latest.target_generation,
            )
            active.append(
                ActiveRiskEpisodeProjection(
                    episode=episode,
                    latest_event=latest,
                    targets=targets,
                )
            )
        if not active:
            return None
        return max(
            active,
            key=lambda item: (
                severity_rank[item.latest_event.severity],
                _aware(item.latest_event.occurred_at),
                item.latest_event.risk_episode_event_id,
            ),
        )

    def targets(
        self,
        risk_episode_id: str,
        *,
        generation: int,
    ) -> tuple[RiskEpisodeTargetRow, ...]:
        return tuple(
            self._session.scalars(
                select(RiskEpisodeTargetRow)
                .where(
                    RiskEpisodeTargetRow.risk_episode_id == risk_episode_id,
                    RiskEpisodeTargetRow.target_generation == generation,
                )
                .order_by(RiskEpisodeTargetRow.symbol)
            )
        )

    def _append_targets(
        self,
        *,
        episode_id: str,
        targets: tuple[RiskTarget, ...],
        config_manifest_hash: str,
        created_at: datetime,
    ) -> None:
        if not targets:
            raise Q1PersistenceConflict("Empty forced-target sets are forbidden")
        for target in targets:
            target_hash = canonical_hash(target)
            target_id = target.target_id or stable_id(
                "q1-risk-target",
                episode_id,
                target.target_generation,
                target.symbol,
                target_hash,
            )
            self._session.add(
                RiskEpisodeTargetRow(
                    risk_target_id=target_id,
                    risk_episode_id=episode_id,
                    symbol=target.symbol,
                    target_generation=target.target_generation,
                    target_quantity=target.target_quantity,
                    trigger_quantity=target.trigger_quantity,
                    trigger_price=target.trigger_price,
                    trigger_quote_id=target.trigger_quote_id,
                    target_weight=target.target_weight,
                    config_manifest_hash=config_manifest_hash,
                    target_hash=target_hash,
                    payload_json=model_payload(target),
                    created_at=created_at,
                )
            )

    @staticmethod
    def _validate_transition(
        *,
        event: RiskEpisodeEvent,
        latest: RiskEpisodeEventRow | None,
    ) -> None:
        if latest is None:
            if (
                event.event_sequence != 1
                or event.event_type is not RiskEpisodeEventType.ACTIVATE
                or event.target_generation != 1
            ):
                raise Q1PersistenceConflict(
                    "First risk episode event must activate generation 1"
                )
            return
        if latest.event_type == RiskEpisodeEventType.RELEASE.value:
            raise Q1PersistenceConflict("Released risk episode cannot accept events")
        if event.event_sequence != latest.event_sequence + 1:
            raise Q1PersistenceConflict("Risk episode event sequence is not contiguous")
        rank = {
            RiskSeverity.NORMAL: 0,
            RiskSeverity.SOFT_STOP: 1,
            RiskSeverity.HARD_REDUCE: 2,
            RiskSeverity.CRITICAL_EXIT: 3,
        }
        previous_severity = RiskSeverity(latest.severity)
        if event.event_type is RiskEpisodeEventType.ESCALATE:
            if not (
                previous_severity is RiskSeverity.HARD_REDUCE
                and event.severity is RiskSeverity.CRITICAL_EXIT
                and event.target_generation == latest.target_generation + 1
            ):
                raise Q1PersistenceConflict(
                    "Only HARD_REDUCE to CRITICAL_EXIT escalation is permitted"
                )
        elif event.target_generation != latest.target_generation:
            raise Q1PersistenceConflict(
                "Only an escalation may change the target generation"
            )
        if (
            event.event_type is not RiskEpisodeEventType.RELEASE
            and rank[event.severity] < rank[previous_severity]
        ):
            raise Q1PersistenceConflict("Risk episode cannot silently downgrade")


class CashSettlementRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def append(self, event: CashSettlementEvent) -> CashSettlementEventRow:
        existing = self._session.scalar(
            select(CashSettlementEventRow).where(
                CashSettlementEventRow.idempotency_key == event.idempotency_key
            )
        )
        if existing is not None:
            if existing.event_hash != event.event_hash:
                raise Q1PersistenceConflict(
                    "Settlement idempotency key has different immutable content"
                )
            return existing
        identity_match = None
        if event.receivable_id is not None:
            identity_match = self._session.scalar(
                select(CashSettlementEventRow).where(
                    CashSettlementEventRow.receivable_id == event.receivable_id,
                    CashSettlementEventRow.event_type == event.event_type.value,
                )
            )
        if identity_match is None and event.source_fill_id is not None:
            identity_match = self._session.scalar(
                select(CashSettlementEventRow).where(
                    CashSettlementEventRow.source_fill_id == event.source_fill_id,
                    CashSettlementEventRow.event_type == event.event_type.value,
                )
            )
        if identity_match is not None:
            if identity_match.event_hash != event.event_hash:
                raise Q1PersistenceConflict(
                    "Settlement economic identity has different immutable content"
                )
            return identity_match
        _require_cycle_fence(
            self._session,
            cycle_id=event.source_cycle_id,
            lease_owner=event.worker_fence_token,
            attempt_count=event.cycle_attempt_count,
            fallback_now=event.created_at,
        )
        row = CashSettlementEventRow(
            cash_settlement_event_id=event.cash_settlement_event_id,
            run_id=event.run_id,
            arm_id=event.arm_id.value,
            source_fill_id=event.source_fill_id,
            calendar_session_id=event.calendar_session_id,
            source_cycle_id=event.source_cycle_id,
            algorithm_version=event.algorithm_version,
            event_type=event.event_type.value,
            receivable_id=event.receivable_id,
            settlement_policy_version=event.settlement_policy_version,
            currency=event.currency,
            settled_cash_delta_usd=event.settled_cash_delta_usd,
            unsettled_receivable_delta_usd=event.unsettled_receivable_delta_usd,
            gross_amount_usd=event.gross_amount_usd,
            commission_usd=event.commission_usd,
            trade_at=event.trade_at,
            settlement_date=event.settlement_date,
            effective_at=event.effective_at,
            worker_fence_token=event.worker_fence_token,
            cycle_attempt_count=event.cycle_attempt_count,
            idempotency_key=event.idempotency_key,
            config_manifest_hash=event.config_manifest_hash,
            code_version=event.code_version,
            model_version=event.model_version,
            source_manifest_hash=event.source_manifest_hash,
            event_hash=event.event_hash,
            payload_json=model_payload(event),
            created_at=event.created_at,
        )
        self._session.add(row)
        return row

    def balances(
        self,
        *,
        run_id: str,
        arm_id: str,
        as_of: datetime,
    ) -> CashBalanceProjection:
        values = self._session.execute(
            select(
                func.coalesce(
                    func.sum(CashSettlementEventRow.settled_cash_delta_usd),
                    0,
                ),
                func.coalesce(
                    func.sum(
                        CashSettlementEventRow.unsettled_receivable_delta_usd
                    ),
                    0,
                ),
            ).where(
                CashSettlementEventRow.run_id == run_id,
                CashSettlementEventRow.arm_id == arm_id,
                CashSettlementEventRow.effective_at <= as_of,
                CashSettlementEventRow.created_at <= as_of,
            )
        ).one()
        return CashBalanceProjection(
            settled_cash_usd=Decimal(values[0]),
            unsettled_receivables_usd=Decimal(values[1]),
        )


class StrategyEvaluationResultRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def append_daily(self, result: StrategyDailyResult) -> StrategyDailyResultRow:
        existing = self._session.scalar(
            select(StrategyDailyResultRow).where(
                StrategyDailyResultRow.run_id == result.run_id,
                StrategyDailyResultRow.arm_id == result.arm_id.value,
                StrategyDailyResultRow.session_date == result.session_date,
            )
        )
        if existing is not None:
            if existing.result_hash != result.result_hash:
                raise Q1PersistenceConflict(
                    "Daily result identity has different immutable content"
                )
            return existing
        row = StrategyDailyResultRow(
            strategy_daily_result_id=result.strategy_daily_result_id,
            evaluation_anchor_id=result.evaluation_anchor_id,
            run_id=result.run_id,
            arm_id=result.arm_id.value,
            calendar_session_id=result.calendar_session_id,
            algorithm_version=result.algorithm_version,
            session_date=result.session_date,
            valuation_at=result.valuation_at,
            nav_usd=result.nav_usd,
            net_daily_return=result.net_daily_return,
            cumulative_return=result.cumulative_return,
            daily_turnover=result.daily_turnover,
            cumulative_turnover=result.cumulative_turnover,
            commissions_usd=result.commissions_usd,
            spread_cost_usd=result.spread_cost_usd,
            delay_cost_usd=result.delay_cost_usd,
            sensitivity_5bp_usd=result.sensitivity_5bp_usd,
            sensitivity_10bp_usd=result.sensitivity_10bp_usd,
            cash_weight=result.cash_weight,
            qqq_weight=result.qqq_weight,
            soxx_weight=result.soxx_weight,
            active_risk_episode_count=result.active_risk_episode_count,
            active_llm_reduction_count=result.active_llm_reduction_count,
            config_manifest_hash=result.config_manifest_hash,
            code_version=result.code_version,
            model_version=result.model_version,
            source_manifest_hash=result.source_manifest_hash,
            result_hash=result.result_hash,
            payload_json=model_payload(result),
            created_at=result.created_at,
        )
        self._session.add(row)
        return row

    def append_matched(
        self,
        result: MatchedAttributionResult,
    ) -> MatchedAttributionResultRow:
        existing = self._session.scalar(
            select(MatchedAttributionResultRow).where(
                MatchedAttributionResultRow.run_id == result.run_id,
                MatchedAttributionResultRow.comparison == result.comparison.value,
                MatchedAttributionResultRow.through_session_date
                == result.through_session_date,
            )
        )
        if existing is not None:
            if existing.result_hash != result.result_hash:
                raise Q1PersistenceConflict(
                    "Matched result identity has different immutable content"
                )
            return existing
        row = MatchedAttributionResultRow(
            matched_attribution_result_id=result.matched_attribution_result_id,
            evaluation_anchor_id=result.evaluation_anchor_id,
            run_id=result.run_id,
            algorithm_version=result.algorithm_version,
            comparison=result.comparison.value,
            left_arm_id=result.left_arm_id.value,
            right_arm_id=result.right_arm_id.value,
            through_session_date=result.through_session_date,
            common_valid_sessions=result.common_valid_sessions,
            mean_daily_difference=result.mean_daily_difference,
            annualized_difference=result.annualized_difference,
            newey_west_lag=result.newey_west_lag,
            newey_west_standard_error=result.newey_west_standard_error,
            bootstrap_seed=result.bootstrap_seed,
            bootstrap_lower=result.bootstrap_lower,
            bootstrap_upper=result.bootstrap_upper,
            promotion_ready=result.promotion_ready,
            config_manifest_hash=result.config_manifest_hash,
            code_version=result.code_version,
            model_version=result.model_version,
            source_manifest_hash=result.source_manifest_hash,
            result_hash=result.result_hash,
            payload_json=model_payload(result),
            created_at=result.created_at,
        )
        self._session.add(row)
        return row

    def daily_results(
        self,
        *,
        run_id: str,
        arm_id: str,
    ) -> list[StrategyDailyResultRow]:
        return list(
            self._session.scalars(
                select(StrategyDailyResultRow)
                .where(
                    StrategyDailyResultRow.run_id == run_id,
                    StrategyDailyResultRow.arm_id == arm_id,
                )
                .order_by(StrategyDailyResultRow.session_date)
            )
        )


def _require_cycle_fence(
    session: Session,
    *,
    cycle_id: str,
    lease_owner: str,
    attempt_count: int,
    fallback_now: datetime,
) -> PaperCycleRow:
    statement: Select[tuple[PaperCycleRow]] = select(PaperCycleRow).where(
        PaperCycleRow.cycle_id == cycle_id
    )
    is_postgresql = (
        session.bind is not None and session.bind.dialect.name == "postgresql"
    )
    if is_postgresql:
        statement = statement.with_for_update()
    row = session.scalar(statement)
    if row is None:
        raise Q1CycleFenceError(f"Unknown source cycle {cycle_id!r}")
    comparison_now = _aware(fallback_now)
    if is_postgresql:
        database_now = session.scalar(select(func.clock_timestamp()))
        if database_now is None:
            raise Q1CycleFenceError("PostgreSQL database clock is unavailable")
        comparison_now = _aware(database_now)
    if (
        row.status != "RUNNING"
        or row.lease_owner != lease_owner
        or row.attempt_count != attempt_count
        or row.lease_expires_at is None
        or _aware(row.lease_expires_at) <= comparison_now
    ):
        raise Q1CycleFenceError(
            f"Cycle lease is not owned by {lease_owner} attempt {attempt_count}"
        )
    return row


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
