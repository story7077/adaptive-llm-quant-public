from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from trading.data.alpaca import FEED, PROVIDER
from trading.data.market_repository import MarketDataRepository
from trading.data.q1_pit import Q1PointInTimeDataError, Q1PointInTimeMarketData
from trading.domain.contracts import Fill
from trading.domain.enums import MarketConnectionState, OrderSide
from trading.domain.hashing import canonical_hash, stable_id
from trading.domain.q1 import CashSettlementEvent, OrderEvent, OrderEventType
from trading.domain.q1_runtime import Q1Fill, Q1OrderIntent
from trading.execution.order_state import (
    OrderAggregate,
    OrderDescriptor,
    OrderEventProvenance,
    append_order_event,
    expire_orders,
    pending_orders,
)
from trading.execution.q1_paper import (
    Q1ExecutionConfig,
    Q1FillEconomics,
    Q1PriceGuardViolation,
    build_q1_fill_economics,
)
from trading.persistence.models import (
    FillRow,
    MarketCalendarSessionRow,
    MarketQuoteRow,
    MarketStreamStatusRow,
    PaperCycleRow,
)
from trading.persistence.q1 import CashSettlementRepository, OrderEventRepository
from trading.persistence.q1_runtime import (
    Q1OrderBook,
    append_arm_state,
    append_fill,
    complete_fenced_cycle,
    latest_arm_state,
    load_q1_order_book,
    require_cycle_fence,
)
from trading.runtime.provenance import workspace_code_version
from trading.runtime.q1_config import (
    adv_lookback_sessions,
    displayed_size_unit_shares,
    execution_config,
    maximum_quote_age_seconds,
    maximum_quote_skew_seconds,
    settlement_policy,
)
from trading.runtime.q1_paper import Q1_MODEL_VERSION, Q1PaperRuntimeService
from trading.runtime.q1_scheduler import (
    VersionedMarketSession,
    is_regular_session_time,
    normal_order_window,
)
from trading.runtime.q1_state import Q1ArmState, UnsettledReceivable
from trading.settlement.service import (
    BusinessCalendar,
    SettlementProvenance,
    record_buy_cash_debit,
    record_sell_receivable,
)

Q1_DAILY_DATASET_VERSION = "alpaca_iex_adjusted_all_v1"
Q1_BASE_EXECUTION_SCENARIO = "Q1_BASE_V1"


class Q1ExecutionCycleError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _QuoteCandidate:
    aggregate: OrderAggregate
    intent: Q1OrderIntent
    quote: MarketQuoteRow
    adv_shares: Decimal
    adv_bar_ids: tuple[str, ...]
    remaining_adv_capacity: Decimal
    cumulative_notional_before: Decimal


@dataclass(frozen=True, slots=True)
class _PreparedMutation:
    aggregate: OrderAggregate
    intent: Q1OrderIntent
    event: OrderEvent
    fill: Q1Fill | None
    state_before_sequence: int | None
    state_after: Q1ArmState | None
    settlement_event: CashSettlementEvent | None


class Q1ExecutionCycleProcessor:
    """Execute Q1 paper intents using only immutable quotes and order events."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        runtime: Q1PaperRuntimeService,
        workspace_root: Path,
    ) -> None:
        self._session_factory = session_factory
        self._runtime = runtime
        self._workspace_root = workspace_root
        self._market = MarketDataRepository(session_factory)
        self._pit = Q1PointInTimeMarketData(session_factory)

    def process(
        self,
        cycle: PaperCycleRow,
        *,
        calendar: VersionedMarketSession,
        now: datetime,
    ) -> dict[str, object]:
        instant = _aware(now)
        book, states, stream_state, calendar_dates = self._read_inputs(
            cycle.run_id,
            calendar=calendar,
            as_of=instant,
        )
        pending = pending_orders(book.descriptors, book.events, as_of=instant)
        intent_by_id = {
            intent.order_intent_id: intent
            for intent in book.intents
        }
        expired = expire_orders(
            orders=book.descriptors,
            events=book.events,
            as_of=instant,
            provenance=self._provenance(
                cycle,
                source_manifest_hash=canonical_hash(
                    {
                        "cycle_id": cycle.cycle_id,
                        "calendar_session_id": calendar.calendar_session_id,
                        "status": "EXPIRY_PREPARATION",
                    }
                ),
            ),
            source_cycle_id=cycle.cycle_id,
            expire_at_boundary=instant >= calendar.close_at,
        )
        expired_ids = {event.order_intent_id for event in expired}
        candidates: list[_QuoteCandidate] = []
        blocked_reasons: dict[str, tuple[str, str | None]] = {}
        adv_manifest: dict[str, object] = {}
        quote_manifest: dict[str, object] = {}
        normal_start, _normal_end = normal_order_window(
            calendar,
            schedule=self._runtime.schedule,
        )
        for aggregate in pending:
            order = aggregate.order
            intent = intent_by_id[order.order_intent_id]
            if order.order_intent_id in expired_ids:
                continue
            if not is_regular_session_time(instant, calendar):
                blocked_reasons[order.order_intent_id] = (
                    "OUTSIDE_ACTUAL_REGULAR_SESSION",
                    None,
                )
                continue
            if order.order_class.value == "NORMAL" and instant < normal_start:
                continue
            if stream_state != MarketConnectionState.CONNECTED.value:
                blocked_reasons[order.order_intent_id] = (
                    "MARKET_STREAM_NOT_CONNECTED",
                    None,
                )
                continue
            cursor = self._quote_cursor(
                aggregate,
                events=book.events,
            )
            quote = self._market.first_executable_quote(
                provider=PROVIDER,
                feed=FEED,
                symbol=order.symbol,
                observed_after=cursor,
                as_of=instant,
                max_age_seconds=maximum_quote_age_seconds(
                    self._runtime.config
                ),
                side=order.side,
            )
            if quote is None:
                blocked_reasons[order.order_intent_id] = (
                    "NO_FRESH_POST_INTENT_EXECUTABLE_QUOTE",
                    None,
                )
                continue
            try:
                adv, bar_ids = self._pit.completed_adv_shares(
                    symbol=order.symbol,
                    current_session_date=calendar.session_date,
                    as_of=instant,
                    lookback_sessions=adv_lookback_sessions(
                        self._runtime.config
                    ),
                    query_limit=(
                        adv_lookback_sessions(self._runtime.config) + 1
                    ),
                    dataset_version=Q1_DAILY_DATASET_VERSION,
                )
            except Q1PointInTimeDataError:
                blocked_reasons[order.order_intent_id] = (
                    "POINT_IN_TIME_ADV_UNAVAILABLE",
                    quote.quote_id,
                )
                continue
            consumed = self._session_consumed_quantity(
                run_id=cycle.run_id,
                arm_id=order.arm_id,
                symbol=order.symbol,
                session_open=calendar.open_at,
                as_of=instant,
            )
            cap = (
                adv * execution_config(self._runtime.config).adv_participation
                - consumed
            )
            cap = max(Decimal("0"), cap)
            cumulative_notional = self._order_cumulative_notional(
                order.order_intent_id
            )
            candidates.append(
                _QuoteCandidate(
                    aggregate=aggregate,
                    intent=intent,
                    quote=quote,
                    adv_shares=adv,
                    adv_bar_ids=bar_ids,
                    remaining_adv_capacity=cap,
                    cumulative_notional_before=cumulative_notional,
                )
            )
            adv_manifest[order.order_intent_id] = {
                "adv_shares": adv,
                "bar_ids": bar_ids,
                "consumed_quantity": consumed,
                "remaining_capacity": cap,
            }
            quote_manifest[order.order_intent_id] = {
                "quote_id": quote.quote_id,
                "event_time": _aware(quote.event_time),
                "available_at": _aware(quote.available_at),
            }
        maximum_skew = Decimal(
            maximum_quote_skew_seconds(self._runtime.config)
        )
        candidates_by_decision: dict[str, list[_QuoteCandidate]] = {}
        for candidate in candidates:
            candidates_by_decision.setdefault(
                candidate.intent.portfolio_decision_id,
                [],
            ).append(candidate)
        skew_blocked_ids: set[str] = set()
        for bundle in candidates_by_decision.values():
            if _quote_skew_seconds(bundle) <= maximum_skew:
                continue
            for candidate in bundle:
                order_id = candidate.intent.order_intent_id
                skew_blocked_ids.add(order_id)
                blocked_reasons[order_id] = (
                    "MULTI_SYMBOL_QUOTE_SKEW_EXCEEDED",
                    candidate.quote.quote_id,
                )
        candidates = [
            candidate
            for candidate in candidates
            if candidate.intent.order_intent_id not in skew_blocked_ids
        ]
        source_manifest_hash = canonical_hash(
            {
                "calendar_session_id": calendar.calendar_session_id,
                "stream_state": stream_state,
                "pending_latest_event_ids": {
                    item.order.order_intent_id: item.latest_event_id
                    for item in pending
                },
                "quotes": quote_manifest,
                "adv": adv_manifest,
                "blocked": blocked_reasons,
                "settlement_calendar_dates": calendar_dates,
            }
        )
        provenance = self._provenance(
            cycle,
            source_manifest_hash=source_manifest_hash,
        )
        mutations = list(
            self._prepare_expiry_mutations(
                expired,
                book_events=book.events,
                provenance=provenance,
                cycle=cycle,
                now=instant,
                intent_by_id=intent_by_id,
            )
        )
        mutations.extend(
            self._prepare_blocked_mutations(
                pending=pending,
                blocked_reasons=blocked_reasons,
                book_events=book.events,
                provenance=provenance,
                cycle=cycle,
                now=instant,
                intent_by_id=intent_by_id,
            )
        )
        mutations.extend(
            self._prepare_fill_mutations(
                candidates=tuple(candidates),
                states=states,
                book_events=book.events,
                provenance=provenance,
                cycle=cycle,
                calendar=calendar,
                calendar_dates=calendar_dates,
                now=instant,
                source_manifest_hash=source_manifest_hash,
            )
        )
        return self._commit(
            cycle,
            mutations=tuple(mutations),
            now=instant,
            calendar=calendar,
            stream_state=stream_state,
            source_manifest_hash=source_manifest_hash,
        )

    def _read_inputs(
        self,
        run_id: str,
        *,
        calendar: VersionedMarketSession,
        as_of: datetime,
    ) -> tuple[
        Q1OrderBook,
        dict[str, Q1ArmState],
        str,
        tuple[date, ...],
    ]:
        with self._session_factory() as session:
            book = load_q1_order_book(session, run_id=run_id)
            arm_ids = {descriptor.arm_id for descriptor in book.descriptors}
            states = {
                arm_id: state
                for arm_id in sorted(arm_ids)
                if (
                    state := latest_arm_state(
                        session,
                        run_id=run_id,
                        arm_id=arm_id,
                    )
                )
                is not None
            }
            status = session.get(MarketStreamStatusRow, (PROVIDER, FEED))
            stream_state = (
                MarketConnectionState.DISCONNECTED.value
                if status is None
                else status.state
            )
            calendar_dates = tuple(
                session.scalars(
                    select(MarketCalendarSessionRow.session_date)
                    .where(
                        MarketCalendarSessionRow.calendar_version
                        == calendar.calendar_version,
                        MarketCalendarSessionRow.available_at <= as_of,
                    )
                    .order_by(MarketCalendarSessionRow.session_date)
                )
            )
        return book, states, stream_state, calendar_dates

    def _quote_cursor(
        self,
        aggregate: OrderAggregate,
        *,
        events: tuple[OrderEvent, ...],
    ) -> datetime:
        quote_ids = tuple(
            event.quote_id
            for event in events
            if (
                event.order_intent_id == aggregate.order.order_intent_id
                and event.quote_id is not None
            )
        )
        if not quote_ids:
            return aggregate.order.created_at
        with self._session_factory() as session:
            rows = tuple(
                session.scalars(
                    select(MarketQuoteRow).where(
                        MarketQuoteRow.quote_id.in_(quote_ids)
                    )
                )
            )
        if len(rows) != len(set(quote_ids)):
            raise Q1ExecutionCycleError(
                "Order-event quote cursor references missing market data"
            )
        return max(
            aggregate.order.created_at,
            *(
                max(_aware(row.event_time), _aware(row.available_at))
                for row in rows
            ),
        )

    def _session_consumed_quantity(
        self,
        *,
        run_id: str,
        arm_id: str,
        symbol: str,
        session_open: datetime,
        as_of: datetime,
    ) -> Decimal:
        with self._session_factory() as session:
            value = session.scalar(
                select(func.coalesce(func.sum(FillRow.quantity), 0)).where(
                    FillRow.run_id == run_id,
                    FillRow.arm_id == arm_id,
                    FillRow.symbol == symbol,
                    FillRow.algorithm_version == "q1_math_core_v1",
                    FillRow.effective_at >= session_open,
                    FillRow.effective_at <= as_of,
                )
            )
        return Decimal("0") if value is None else Decimal(value)

    def _order_cumulative_notional(self, order_intent_id: str) -> Decimal:
        with self._session_factory() as session:
            rows = session.execute(
                select(FillRow.quantity, FillRow.price).where(
                    FillRow.order_intent_id == order_intent_id,
                    FillRow.algorithm_version == "q1_math_core_v1",
                )
            ).all()
        return sum(
            (
                Decimal(quantity) * Decimal(price)
                for quantity, price in rows
                if quantity is not None and price is not None
            ),
            Decimal("0"),
        )

    def _prepare_expiry_mutations(
        self,
        expired: tuple[OrderEvent, ...],
        *,
        book_events: tuple[OrderEvent, ...],
        provenance: OrderEventProvenance,
        cycle: PaperCycleRow,
        now: datetime,
        intent_by_id: dict[str, Q1OrderIntent],
    ) -> tuple[_PreparedMutation, ...]:
        result: list[_PreparedMutation] = []
        for provisional in expired:
            intent = intent_by_id[provisional.order_intent_id]
            event = append_order_event(
                order=_descriptor(intent),
                existing_events=book_events,
                event_type=OrderEventType.EXPIRED,
                occurred_at=now,
                available_at=now,
                provenance=provenance,
                reason="ORDER_VALIDITY_WINDOW_ENDED",
                source_cycle_id=cycle.cycle_id,
            )
            result.append(
                _PreparedMutation(
                    aggregate=_aggregate_for(intent, book_events),
                    intent=intent,
                    event=event,
                    fill=None,
                    state_before_sequence=None,
                    state_after=None,
                    settlement_event=None,
                )
            )
        return tuple(result)

    def _prepare_blocked_mutations(
        self,
        *,
        pending: tuple[OrderAggregate, ...],
        blocked_reasons: dict[str, tuple[str, str | None]],
        book_events: tuple[OrderEvent, ...],
        provenance: OrderEventProvenance,
        cycle: PaperCycleRow,
        now: datetime,
        intent_by_id: dict[str, Q1OrderIntent],
    ) -> tuple[_PreparedMutation, ...]:
        result: list[_PreparedMutation] = []
        for aggregate in pending:
            reason_quote = blocked_reasons.get(
                aggregate.order.order_intent_id
            )
            if reason_quote is None:
                continue
            reason, quote_id = reason_quote
            intent = intent_by_id[aggregate.order.order_intent_id]
            result.append(
                _PreparedMutation(
                    aggregate=aggregate,
                    intent=intent,
                    event=append_order_event(
                        order=aggregate.order,
                        existing_events=book_events,
                        event_type=OrderEventType.BLOCKED_BY_DATA,
                        occurred_at=now,
                        available_at=now,
                        provenance=provenance,
                        reason=reason,
                        quote_id=quote_id,
                        source_cycle_id=cycle.cycle_id,
                    ),
                    fill=None,
                    state_before_sequence=None,
                    state_after=None,
                    settlement_event=None,
                )
            )
        return tuple(result)

    def _prepare_fill_mutations(
        self,
        *,
        candidates: tuple[_QuoteCandidate, ...],
        states: dict[str, Q1ArmState],
        book_events: tuple[OrderEvent, ...],
        provenance: OrderEventProvenance,
        cycle: PaperCycleRow,
        calendar: VersionedMarketSession,
        calendar_dates: tuple[date, ...],
        now: datetime,
        source_manifest_hash: str,
    ) -> tuple[_PreparedMutation, ...]:
        config = execution_config(self._runtime.config)
        policy = settlement_policy(self._runtime.config)
        business_calendar = BusinessCalendar(
            version=calendar.calendar_version,
            sessions=tuple(calendar_dates),
        )
        settlement_provenance = SettlementProvenance(
            run_id=cycle.run_id,
            source_cycle_id=cycle.cycle_id,
            config_manifest_hash=self._runtime.config.manifest_hash,
            code_version=workspace_code_version(self._workspace_root),
            model_version=Q1_MODEL_VERSION,
            source_manifest_hash=source_manifest_hash,
            worker_fence_token=_lease_owner(cycle),
            cycle_attempt_count=cycle.attempt_count,
        )
        working_states = dict(states)
        remaining_cap_by_arm_symbol: dict[tuple[str, str], Decimal] = {}
        result: list[_PreparedMutation] = []
        ordered = sorted(
            candidates,
            key=lambda item: (
                0 if item.intent.side is OrderSide.SELL else 1,
                item.intent.arm_id.value,
                item.intent.symbol,
                item.intent.order_intent_id,
            ),
        )
        for candidate in ordered:
            intent = candidate.intent
            aggregate = candidate.aggregate
            state = working_states.get(intent.arm_id.value)
            if state is None:
                result.append(
                    self._blocked_candidate(
                        candidate,
                        book_events=book_events,
                        provenance=provenance,
                        cycle=cycle,
                        now=now,
                        reason="ARM_STATE_UNAVAILABLE",
                    )
                )
                continue
            key = (intent.arm_id.value, intent.symbol)
            remaining_adv = remaining_cap_by_arm_symbol.setdefault(
                key,
                candidate.remaining_adv_capacity,
            )
            side_quantity = Decimal(
                candidate.quote.ask_size_round_lots
                if intent.side is OrderSide.BUY
                else candidate.quote.bid_size_round_lots
            ) * displayed_size_unit_shares(self._runtime.config)
            executable_remaining = aggregate.remaining_quantity
            if intent.side is OrderSide.SELL:
                executable_remaining = min(
                    executable_remaining,
                    state.positions.get(intent.symbol, Decimal("0")),
                )
            else:
                executable_remaining = min(
                    executable_remaining,
                    _maximum_affordable_quantity(
                        settled_cash=state.settled_cash_usd,
                        ask_price=candidate.quote.ask_price,
                        cumulative_notional_before=(
                            candidate.cumulative_notional_before
                        ),
                        cumulative_commission_before=(
                            aggregate.cumulative_commission_usd
                        ),
                        config=config,
                    ),
                )
            if executable_remaining <= 0 or remaining_adv <= 0:
                result.append(
                    self._blocked_candidate(
                        candidate,
                        book_events=book_events,
                        provenance=provenance,
                        cycle=cycle,
                        now=now,
                        reason="EXECUTION_CAPACITY_UNAVAILABLE",
                    )
                )
                continue
            try:
                economics = build_q1_fill_economics(
                    side=intent.side,
                    remaining_quantity=executable_remaining,
                    bid_price=candidate.quote.bid_price,
                    ask_price=candidate.quote.ask_price,
                    executable_side_quantity=side_quantity,
                    remaining_adv_capacity=remaining_adv,
                    decision_reference_price=(
                        intent.decision_reference_price
                    ),
                    decision_spread_bps=intent.decision_spread_bps,
                    cumulative_notional_before=(
                        candidate.cumulative_notional_before
                    ),
                    cumulative_commission_before=(
                        aggregate.cumulative_commission_usd
                    ),
                    config=config,
                )
            except Q1PriceGuardViolation:
                result.append(
                    self._blocked_candidate(
                        candidate,
                        book_events=book_events,
                        provenance=provenance,
                        cycle=cycle,
                        now=now,
                        reason="DYNAMIC_DECISION_PRICE_GUARD",
                        event_type=OrderEventType.BLOCKED_BY_PRICE_GUARD,
                    )
                )
                continue
            except ValueError:
                result.append(
                    self._blocked_candidate(
                        candidate,
                        book_events=book_events,
                        provenance=provenance,
                        cycle=cycle,
                        now=now,
                        reason="INVALID_EXECUTION_CAPACITY_OR_QUOTE",
                    )
                )
                continue
            if (
                intent.side is OrderSide.BUY
                and economics.quantity * economics.price
                + economics.commission_usd
                > state.settled_cash_usd
            ):
                raise Q1ExecutionCycleError(
                    "Settled-cash affordability guard failed"
                )
            fill = _q1_fill(
                cycle=cycle,
                intent=intent,
                candidate=candidate,
                economics=economics,
                now=now,
                config_manifest_hash=self._runtime.config.manifest_hash,
                code_version=workspace_code_version(self._workspace_root),
                source_manifest_hash=source_manifest_hash,
            )
            settlement_event = (
                record_buy_cash_debit(
                    arm_id=intent.arm_id,
                    fill_id=fill.fill_id,
                    trade_at=now,
                    fill_notional_usd=fill.quantity * fill.price,
                    commission_usd=fill.commission_usd,
                    created_at=now,
                    calendar_session_id=calendar.calendar_session_id,
                    policy=policy,
                    provenance=settlement_provenance,
                )
                if intent.side is OrderSide.BUY
                else record_sell_receivable(
                    arm_id=intent.arm_id,
                    fill_id=fill.fill_id,
                    trade_at=now,
                    trade_session=calendar.session_date,
                    fill_notional_usd=fill.quantity * fill.price,
                    commission_usd=fill.commission_usd,
                    created_at=now,
                    calendar_session_id=calendar.calendar_session_id,
                    policy=policy,
                    calendar=business_calendar,
                    provenance=settlement_provenance,
                )
            )
            receivable = (
                None
                if intent.side is OrderSide.BUY
                else UnsettledReceivable(
                    receivable_id=_required_receivable_id(
                        settlement_event.receivable_id
                    ),
                    source_fill_id=fill.fill_id,
                    amount_usd=(
                        settlement_event.unsettled_receivable_delta_usd
                    ),
                    settlement_date=_required_settlement_date(
                        settlement_event.settlement_date
                    ),
                    created_at=now,
                )
            )
            state_after = state.apply_fill(
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
            working_states[intent.arm_id.value] = state_after
            remaining_cap_by_arm_symbol[key] = (
                remaining_adv - economics.quantity
            )
            event_type = (
                OrderEventType.FILLED
                if economics.quantity == aggregate.remaining_quantity
                else OrderEventType.PARTIALLY_FILLED
            )
            event = append_order_event(
                order=aggregate.order,
                existing_events=book_events,
                event_type=event_type,
                occurred_at=now,
                available_at=now,
                provenance=provenance,
                quantity_delta=economics.quantity,
                commission_delta_usd=economics.commission_usd,
                source_id=fill.fill_id,
                quote_id=candidate.quote.quote_id,
                source_cycle_id=cycle.cycle_id,
            )
            result.append(
                _PreparedMutation(
                    aggregate=aggregate,
                    intent=intent,
                    event=event,
                    fill=fill,
                    state_before_sequence=state.sequence,
                    state_after=state_after,
                    settlement_event=settlement_event,
                )
            )
        return tuple(result)

    def _blocked_candidate(
        self,
        candidate: _QuoteCandidate,
        *,
        book_events: tuple[OrderEvent, ...],
        provenance: OrderEventProvenance,
        cycle: PaperCycleRow,
        now: datetime,
        reason: str,
        event_type: OrderEventType = OrderEventType.BLOCKED_BY_DATA,
    ) -> _PreparedMutation:
        return _PreparedMutation(
            aggregate=candidate.aggregate,
            intent=candidate.intent,
            event=append_order_event(
                order=candidate.aggregate.order,
                existing_events=book_events,
                event_type=event_type,
                occurred_at=now,
                available_at=now,
                provenance=provenance,
                reason=reason,
                quote_id=candidate.quote.quote_id,
                source_cycle_id=cycle.cycle_id,
            ),
            fill=None,
            state_before_sequence=None,
            state_after=None,
            settlement_event=None,
        )

    def _commit(
        self,
        cycle: PaperCycleRow,
        *,
        mutations: tuple[_PreparedMutation, ...],
        now: datetime,
        calendar: VersionedMarketSession,
        stream_state: str,
        source_manifest_hash: str,
    ) -> dict[str, object]:
        with self._session_factory.begin() as session:
            locked = require_cycle_fence(
                session,
                cycle_id=cycle.cycle_id,
                lease_owner=_lease_owner(cycle),
                attempt_count=cycle.attempt_count,
                fallback_now=now,
            )
            book = load_q1_order_book(session, run_id=cycle.run_id)
            latest_by_id = {
                aggregate.order.order_intent_id: aggregate
                for aggregate in pending_orders(
                    book.descriptors,
                    book.events,
                    as_of=now,
                )
            }
            order_repository = OrderEventRepository(session)
            cash_repository = CashSettlementRepository(session)
            fill_ids: list[str] = []
            event_ids: list[str] = []
            state_sequences: dict[str, int] = {}
            for mutation in mutations:
                actual = latest_by_id.get(
                    mutation.intent.order_intent_id
                )
                if (
                    actual is None
                    or actual.latest_event_id
                    != mutation.aggregate.latest_event_id
                ):
                    raise Q1ExecutionCycleError(
                        "Order state changed during execution preparation"
                    )
                if mutation.fill is not None:
                    state = latest_arm_state(
                        session,
                        run_id=cycle.run_id,
                        arm_id=mutation.intent.arm_id.value,
                        lock=True,
                    )
                    if (
                        state is None
                        or mutation.state_before_sequence is None
                        or state.sequence != mutation.state_before_sequence
                        or mutation.state_after is None
                    ):
                        raise Q1ExecutionCycleError(
                            "Arm state changed during execution preparation"
                        )
                    append_fill(session, mutation.fill)
                    if mutation.settlement_event is None:
                        raise Q1ExecutionCycleError(
                            "Fill is missing a settlement event"
                        )
                    cash_repository.append(mutation.settlement_event)
                    append_arm_state(
                        session,
                        run_id=cycle.run_id,
                        state=mutation.state_after,
                        source_cycle_id=cycle.cycle_id,
                        created_at=now,
                        expected_previous_sequence=state.sequence,
                    )
                    fill_ids.append(mutation.fill.fill_id)
                    state_sequences[mutation.intent.arm_id.value] = (
                        mutation.state_after.sequence
                    )
                order_repository.append(mutation.event)
                event_ids.append(mutation.event.event_id)
            output: dict[str, object] = {
                "status": (
                    "Q1_EXECUTION_EVENTS_COMMITTED"
                    if mutations
                    else "NO_PENDING_EXECUTION"
                ),
                "fill_ids": fill_ids,
                "order_event_ids": event_ids,
                "state_sequences": state_sequences,
                "stream_state": stream_state,
                "real_order_routing": False,
            }
            complete_fenced_cycle(
                locked,
                cutoff=now,
                input_manifest={
                    "cycle_id": cycle.cycle_id,
                    "calendar_session_id": calendar.calendar_session_id,
                    "source_manifest_hash": source_manifest_hash,
                    "config_manifest_hash": (
                        self._runtime.config.manifest_hash
                    ),
                    "real_order_routing": False,
                },
                output_manifest=output,
                completed_at=now,
            )
            return output

    def _provenance(
        self,
        cycle: PaperCycleRow,
        *,
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


def _maximum_affordable_quantity(
    *,
    settled_cash: Decimal,
    ask_price: Decimal,
    cumulative_notional_before: Decimal,
    cumulative_commission_before: Decimal,
    config: Q1ExecutionConfig,
) -> Decimal:
    delay = config.delay_penalty_bps / Decimal("10000")
    fill_price = (ask_price * (Decimal("1") + delay)).quantize(
        config.price_precision,
        rounding=ROUND_HALF_EVEN,
    )
    conservative_cash = max(
        Decimal("0"),
        settled_cash - config.commission_precision,
    )
    denominator = fill_price * (Decimal("1") + config.commission_rate)
    quantity = (conservative_cash / denominator).quantize(
        config.quantity_precision,
        rounding=ROUND_DOWN,
    )
    while quantity > 0:
        notional = quantity * fill_price
        cumulative = cumulative_notional_before + notional
        total_commission = (
            Decimal("0")
            if cumulative <= config.commission_waiver_threshold_usd
            else (cumulative * config.commission_rate).quantize(
                config.commission_precision,
                rounding=ROUND_HALF_EVEN,
            )
        )
        incremental = total_commission - cumulative_commission_before
        if notional + max(Decimal("0"), incremental) <= settled_cash:
            return quantity
        quantity -= config.quantity_precision
    return Decimal("0")


def _q1_fill(
    *,
    cycle: PaperCycleRow,
    intent: Q1OrderIntent,
    candidate: _QuoteCandidate,
    economics: Q1FillEconomics,
    now: datetime,
    config_manifest_hash: str,
    code_version: str,
    source_manifest_hash: str,
) -> Q1Fill:
    fill_id = stable_id(
        "q1-fill",
        intent.order_intent_id,
        candidate.quote.quote_id,
        Q1_BASE_EXECUTION_SCENARIO,
    )
    content: dict[str, object] = {
        "fill_id": fill_id,
        "order_intent_id": intent.order_intent_id,
        "run_id": intent.run_id,
        "arm_id": intent.arm_id,
        "source_cycle_id": cycle.cycle_id,
        "quote_id": candidate.quote.quote_id,
        "quote_event_time": _aware(candidate.quote.event_time),
        "quote_available_at": _aware(candidate.quote.available_at),
        "symbol": intent.symbol,
        "side": intent.side,
        "quantity": economics.quantity,
        "price": economics.price,
        "commission_usd": economics.commission_usd,
        "cumulative_order_commission_usd": (
            economics.cumulative_commission_usd
        ),
        "execution_scenario_id": Q1_BASE_EXECUTION_SCENARIO,
        "base_fill_cost_usd": economics.base_execution_cost_usd,
        "sensitivity_5bp_cost_usd": _sensitivity(economics, "5.0"),
        "sensitivity_10bp_cost_usd": _sensitivity(economics, "10.0"),
        "effective_at": now,
        "created_at": now,
        "algorithm_version": "q1_math_core_v1",
        "config_manifest_hash": config_manifest_hash,
        "code_version": code_version,
        "model_version": Q1_MODEL_VERSION,
        "source_manifest_hash": source_manifest_hash,
    }
    return Q1Fill(
        fill_id=fill_id,
        order_intent_id=intent.order_intent_id,
        run_id=intent.run_id,
        arm_id=intent.arm_id,
        source_cycle_id=cycle.cycle_id,
        quote_id=candidate.quote.quote_id,
        quote_event_time=_aware(candidate.quote.event_time),
        quote_available_at=_aware(candidate.quote.available_at),
        symbol=intent.symbol,
        side=intent.side,
        quantity=economics.quantity,
        price=economics.price,
        commission_usd=economics.commission_usd,
        cumulative_order_commission_usd=(
            economics.cumulative_commission_usd
        ),
        execution_scenario_id=Q1_BASE_EXECUTION_SCENARIO,
        base_fill_cost_usd=economics.base_execution_cost_usd,
        sensitivity_5bp_cost_usd=_sensitivity(economics, "5.0"),
        sensitivity_10bp_cost_usd=_sensitivity(economics, "10.0"),
        effective_at=now,
        created_at=now,
        algorithm_version="q1_math_core_v1",
        config_manifest_hash=config_manifest_hash,
        code_version=code_version,
        model_version=Q1_MODEL_VERSION,
        source_manifest_hash=source_manifest_hash,
        fill_hash=canonical_hash(content),
    )


def _sensitivity(economics: Q1FillEconomics, bps: str) -> Decimal:
    key = f"plus_{bps}_bps"
    value = economics.sensitivity_costs_usd.get(key)
    if value is None:
        raise Q1ExecutionCycleError(
            f"Q1 execution sensitivity {bps} bps is not configured"
        )
    return value


def _quote_skew_seconds(candidates: list[_QuoteCandidate]) -> Decimal:
    event_times = [_aware(item.quote.event_time) for item in candidates]
    if not event_times:
        return Decimal("0")
    return Decimal(
        str((max(event_times) - min(event_times)).total_seconds())
    )


def _descriptor(intent: Q1OrderIntent) -> OrderDescriptor:
    from trading.execution.order_state import Q1OrderClass

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


def _aggregate_for(
    intent: Q1OrderIntent,
    events: tuple[OrderEvent, ...],
) -> OrderAggregate:
    from trading.execution.order_state import reduce_order_events

    return reduce_order_events(_descriptor(intent), events)


def _required_receivable_id(value: str | None) -> str:
    if value is None:
        raise Q1ExecutionCycleError("SELL settlement has no receivable ID")
    return value


def _required_settlement_date(value: date | None) -> date:
    if value is None:
        raise Q1ExecutionCycleError("SELL settlement has no settlement date")
    return value


def _lease_owner(cycle: PaperCycleRow) -> str:
    if cycle.lease_owner is None:
        raise Q1ExecutionCycleError("Q1 execution cycle has no lease owner")
    return cycle.lease_owner


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
