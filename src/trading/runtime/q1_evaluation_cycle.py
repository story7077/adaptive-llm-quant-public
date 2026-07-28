from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trading.data.alpaca import FEED, PROVIDER
from trading.domain.enums import OrderSide
from trading.domain.hashing import canonical_data, canonical_hash, stable_id
from trading.domain.q1 import (
    MatchedAttributionResult,
    MatchedComparison,
    OrderEvent,
    Q1ArmId,
    Q1StrategyDecision,
    StrategyDailyResult,
    StrategyEvaluationAnchor,
)
from trading.evaluation.matched import (
    DailyEvaluationObservation,
    MatchedAttribution,
    PerformanceMetrics,
    evaluate_matched_attribution,
    evaluate_performance,
)
from trading.execution.order_state import (
    OrderEventProvenance,
    append_order_event,
    expire_orders,
)
from trading.persistence.models import (
    FillRow,
    MarketQuoteRow,
    PaperCycleRow,
    PortfolioDecisionRow,
    RiskEpisodeEventRow,
    RiskEpisodeRow,
    StrategyDailyResultRow,
)
from trading.persistence.q1 import (
    OrderEventRepository,
    StrategyEvaluationAnchorRepository,
    StrategyEvaluationResultRepository,
)
from trading.persistence.q1_runtime import (
    Q1OrderBook,
    complete_fenced_cycle,
    latest_arm_state,
    load_q1_order_book,
    require_cycle_fence,
)
from trading.runtime.provenance import workspace_code_version
from trading.runtime.q1_config import (
    evaluation_closing_quote_max_age_seconds,
    evaluation_config,
    maximum_quote_skew_seconds,
)
from trading.runtime.q1_paper import Q1_MODEL_VERSION, Q1PaperRuntimeService
from trading.runtime.q1_scheduler import VersionedMarketSession
from trading.runtime.q1_state import Q1ArmState


class Q1EvaluationCycleError(RuntimeError):
    pass


class Q1EvaluationDataNotReady(Q1EvaluationCycleError):
    pass


@dataclass(frozen=True, slots=True)
class _DailyPrepared:
    result: StrategyDailyResult
    observation: DailyEvaluationObservation


class Q1EvaluationCycleProcessor:
    """Persist common-close daily results and the two valid matched comparisons."""

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

    def process(
        self,
        cycle: PaperCycleRow,
        *,
        calendar: VersionedMarketSession,
        now: datetime,
    ) -> dict[str, object]:
        instant = _aware(now)
        if instant < calendar.close_at:
            raise Q1EvaluationCycleError(
                "Daily evaluation cannot precede the actual session close"
            )
        anchor, states, book = self._read_base(cycle.run_id)
        clean_arm_ids = {
            Q1ArmId.B0_CASH.value,
            Q1ArmId.B0_QQQ.value,
            Q1ArmId.B0_VOL.value,
            Q1ArmId.Q1_DET.value,
            Q1ArmId.Q1_LLM.value,
        }
        clean_symbols = tuple(
            sorted(
                {
                    symbol
                    for arm_id, state in states.items()
                    if arm_id in clean_arm_ids
                    for symbol, quantity in state.positions.items()
                    if quantity > 0
                }
            )
        )
        unexpected_clean_symbols = sorted(
            set(clean_symbols) - {"QQQ", "SOXX"}
        )
        if unexpected_clean_symbols:
            raise Q1EvaluationCycleError(
                "Clean strategy arm contains symbols outside Q1 universe: "
                f"{unexpected_clean_symbols}"
            )
        clean_quotes = self._closing_quotes(
            symbols=clean_symbols,
            calendar=calendar,
            available_as_of=instant,
        )
        quotes_by_arm = {
            arm_id: clean_quotes
            for arm_id in states
            if arm_id in clean_arm_ids
        }
        unavailable_arms: dict[str, str] = {}
        for arm_id in (
            Q1ArmId.HOLD.value,
            Q1ArmId.LIVE_MIRROR.value,
        ):
            state = states.get(arm_id)
            if state is None:
                continue
            symbols = tuple(
                sorted(
                    symbol
                    for symbol, quantity in state.positions.items()
                    if quantity > 0
                )
            )
            try:
                quotes_by_arm[arm_id] = self._closing_quotes(
                    symbols=symbols,
                    calendar=calendar,
                    available_as_of=instant,
                )
            except Q1EvaluationDataNotReady as error:
                unavailable_arms[arm_id] = str(error)
        prices_by_arm = {
            arm_id: {
                symbol: (
                    row.bid_price + row.ask_price
                )
                / Decimal("2")
                for symbol, row in quotes.items()
            }
            for arm_id, quotes in quotes_by_arm.items()
        }
        quote_manifest_by_arm = {
            arm_id: {
                symbol: {
                    "quote_id": row.quote_id,
                    "event_time": _aware(row.event_time),
                    "available_at": _aware(row.available_at),
                    "midpoint": prices_by_arm[arm_id][symbol],
                }
                for symbol, row in sorted(quotes.items())
            }
            for arm_id, quotes in sorted(quotes_by_arm.items())
        }
        code_version = workspace_code_version(self._workspace_root)
        source_manifest_hash = canonical_hash(
            {
                "calendar_session_id": calendar.calendar_session_id,
                "valuation_at": calendar.close_at,
                "quotes_by_arm": quote_manifest_by_arm,
                "unavailable_arms": unavailable_arms,
                "state_sequences": {
                    arm_id: state.sequence
                    for arm_id, state in sorted(states.items())
                },
            }
        )
        prepared = self._prepare_daily_results(
            cycle=cycle,
            calendar=calendar,
            anchor=anchor,
            states=states,
            prices_by_arm=prices_by_arm,
            now=instant,
            code_version=code_version,
            source_manifest_hash=source_manifest_hash,
        )
        all_observations = self._observations_with_prepared(
            run_id=cycle.run_id,
            prepared=prepared,
        )
        performance = {
            arm_id: evaluate_performance(
                observations,
                evaluation_config(self._runtime.config),
            )
            for arm_id, observations in all_observations.items()
            if observations
        }
        matched = self._prepare_matched(
            cycle=cycle,
            calendar=calendar,
            anchor=anchor,
            observations=all_observations,
            now=instant,
            code_version=code_version,
            source_manifest_hash=source_manifest_hash,
        )
        expiry_events = self._expiry_events(
            cycle=cycle,
            book=book,
            now=instant,
            source_manifest_hash=source_manifest_hash,
        )
        return self._commit(
            cycle,
            calendar=calendar,
            anchor=anchor,
            prepared=prepared,
            matched=matched,
            performance=performance,
            expiry_events=expiry_events,
            states=states,
            now=instant,
            source_manifest_hash=source_manifest_hash,
            unavailable_arms=unavailable_arms,
        )

    def _read_base(
        self,
        run_id: str,
    ) -> tuple[
        StrategyEvaluationAnchor,
        dict[str, Q1ArmState],
        Q1OrderBook,
    ]:
        with self._session_factory() as session:
            anchor_row = StrategyEvaluationAnchorRepository(session).for_run(
                run_id
            )
            if anchor_row is None:
                raise Q1EvaluationCycleError(
                    "Evaluation anchor is not established"
                )
            anchor = StrategyEvaluationAnchor.model_validate(
                anchor_row.payload_json
            )
            states: dict[str, Q1ArmState] = {}
            for arm_id in Q1ArmId:
                state = latest_arm_state(
                    session,
                    run_id=run_id,
                    arm_id=arm_id.value,
                )
                if state is not None:
                    states[arm_id.value] = state
            book = load_q1_order_book(session, run_id=run_id)
        if not states:
            raise Q1EvaluationCycleError("No Q1 arm states are initialized")
        return anchor, states, book

    def _closing_quotes(
        self,
        *,
        symbols: tuple[str, ...],
        calendar: VersionedMarketSession,
        available_as_of: datetime,
    ) -> dict[str, MarketQuoteRow]:
        if not symbols:
            return {}
        rows: dict[str, MarketQuoteRow] = {}
        with self._session_factory() as session:
            for symbol in symbols:
                row = session.scalar(
                    select(MarketQuoteRow)
                    .where(
                        MarketQuoteRow.provider == PROVIDER,
                        MarketQuoteRow.feed == FEED,
                        MarketQuoteRow.symbol == symbol,
                        MarketQuoteRow.event_time < calendar.close_at,
                        MarketQuoteRow.available_at <= available_as_of,
                        MarketQuoteRow.bid_price > 0,
                        MarketQuoteRow.ask_price > 0,
                        MarketQuoteRow.ask_price
                        >= MarketQuoteRow.bid_price,
                    )
                    .order_by(
                        MarketQuoteRow.event_time.desc(),
                        MarketQuoteRow.available_at.desc(),
                        MarketQuoteRow.quote_id.desc(),
                    )
                    .limit(1)
                )
                if row is None:
                    raise Q1EvaluationDataNotReady(
                        f"Closing quote is unavailable for {symbol}"
                    )
                age = (
                    calendar.close_at - _aware(row.event_time)
                ).total_seconds()
                if (
                    age < 0
                    or age
                    > evaluation_closing_quote_max_age_seconds(
                        self._runtime.config
                    )
                ):
                    raise Q1EvaluationDataNotReady(
                        f"Closing quote is stale for {symbol}"
                    )
                rows[symbol] = row
        event_times = [_aware(row.event_time) for row in rows.values()]
        if (
            event_times
            and (max(event_times) - min(event_times)).total_seconds()
            > maximum_quote_skew_seconds(self._runtime.config)
        ):
            raise Q1EvaluationDataNotReady(
                "Closing quote bundle exceeds maximum skew"
            )
        return rows

    def _prepare_daily_results(
        self,
        *,
        cycle: PaperCycleRow,
        calendar: VersionedMarketSession,
        anchor: StrategyEvaluationAnchor,
        states: dict[str, Q1ArmState],
        prices_by_arm: dict[str, dict[str, Decimal]],
        now: datetime,
        code_version: str,
        source_manifest_hash: str,
    ) -> dict[str, _DailyPrepared]:
        prepared: dict[str, _DailyPrepared] = {}
        for arm_id, state in sorted(states.items()):
            prices = prices_by_arm.get(arm_id)
            if prices is None:
                continue
            enum_arm = Q1ArmId(arm_id)
            nav = state.nav(prices)
            if nav <= 0:
                raise Q1EvaluationCycleError(
                    f"{arm_id} closing NAV must be positive"
                )
            previous = self._previous_daily_result(
                run_id=cycle.run_id,
                arm_id=arm_id,
                before=calendar.session_date,
            )
            previous_nav = (
                anchor.initial_nav_usd
                if previous is None
                else previous.nav_usd
            )
            previous_turnover = (
                Decimal("0")
                if previous is None
                else previous.cumulative_turnover
            )
            economics = self._session_economics(
                run_id=cycle.run_id,
                arm_id=arm_id,
                calendar=calendar,
                previous_nav=previous_nav,
            )
            weights = _evaluation_weights(state, prices)
            risk_count = self._risk_episode_count(
                run_id=cycle.run_id,
                arm_id=arm_id,
                calendar=calendar,
            )
            llm_count = self._llm_reduction_count(
                run_id=cycle.run_id,
                arm_id=enum_arm,
                calendar=calendar,
            )
            net_daily_return = nav / previous_nav - Decimal("1")
            cumulative_return = (
                nav / anchor.initial_nav_usd - Decimal("1")
            )
            cumulative_turnover = (
                previous_turnover + economics["turnover"]
            )
            values: dict[str, object] = {
                "evaluation_anchor_id": anchor.evaluation_anchor_id,
                "run_id": cycle.run_id,
                "arm_id": enum_arm,
                "calendar_session_id": calendar.calendar_session_id,
                "session_date": calendar.session_date,
                "valuation_at": calendar.close_at,
                "nav_usd": nav,
                "net_daily_return": net_daily_return,
                "cumulative_return": cumulative_return,
                "daily_turnover": economics["turnover"],
                "cumulative_turnover": cumulative_turnover,
                "commissions_usd": economics["commissions"],
                "spread_cost_usd": economics["spread"],
                "delay_cost_usd": economics["delay"],
                "sensitivity_5bp_usd": economics["sensitivity_5bp"],
                "sensitivity_10bp_usd": economics[
                    "sensitivity_10bp"
                ],
                "cash_weight": weights["USD_CASH"],
                "qqq_weight": weights.get("QQQ", Decimal("0")),
                "soxx_weight": weights.get("SOXX", Decimal("0")),
                "active_risk_episode_count": risk_count,
                "active_llm_reduction_count": llm_count,
                "algorithm_version": "q1_math_core_v1",
                "config_manifest_hash": (
                    self._runtime.config.manifest_hash
                ),
                "code_version": code_version,
                "model_version": Q1_MODEL_VERSION,
                "source_manifest_hash": source_manifest_hash,
                "created_at": now,
            }
            result_hash = canonical_hash(values)
            result = StrategyDailyResult(
                strategy_daily_result_id=stable_id(
                    "q1-daily-result",
                    cycle.run_id,
                    arm_id,
                    calendar.session_date,
                    result_hash,
                ),
                evaluation_anchor_id=anchor.evaluation_anchor_id,
                run_id=cycle.run_id,
                arm_id=enum_arm,
                calendar_session_id=calendar.calendar_session_id,
                session_date=calendar.session_date,
                valuation_at=calendar.close_at,
                nav_usd=nav,
                net_daily_return=net_daily_return,
                cumulative_return=cumulative_return,
                daily_turnover=economics["turnover"],
                cumulative_turnover=cumulative_turnover,
                commissions_usd=economics["commissions"],
                spread_cost_usd=economics["spread"],
                delay_cost_usd=economics["delay"],
                sensitivity_5bp_usd=economics["sensitivity_5bp"],
                sensitivity_10bp_usd=economics[
                    "sensitivity_10bp"
                ],
                cash_weight=weights["USD_CASH"],
                qqq_weight=weights.get("QQQ", Decimal("0")),
                soxx_weight=weights.get("SOXX", Decimal("0")),
                active_risk_episode_count=risk_count,
                active_llm_reduction_count=llm_count,
                algorithm_version="q1_math_core_v1",
                config_manifest_hash=self._runtime.config.manifest_hash,
                code_version=code_version,
                model_version=Q1_MODEL_VERSION,
                source_manifest_hash=source_manifest_hash,
                result_hash=result_hash,
                created_at=now,
            )
            observation = DailyEvaluationObservation(
                session_date=result.session_date,
                arm_id=result.arm_id,
                net_daily_return=result.net_daily_return,
                daily_turnover=result.daily_turnover,
                commissions_usd=result.commissions_usd,
                spread_cost_usd=result.spread_cost_usd,
                delay_cost_usd=result.delay_cost_usd,
                sensitivity_5bp_usd=result.sensitivity_5bp_usd,
                sensitivity_10bp_usd=result.sensitivity_10bp_usd,
                cash_weight=result.cash_weight,
                qqq_weight=result.qqq_weight,
                soxx_weight=result.soxx_weight,
                risk_episode_active=risk_count > 0,
                llm_reduction_active=llm_count > 0,
            )
            prepared[arm_id] = _DailyPrepared(
                result=result,
                observation=observation,
            )
        return prepared

    def _previous_daily_result(
        self,
        *,
        run_id: str,
        arm_id: str,
        before: date,
    ) -> StrategyDailyResultRow | None:
        with self._session_factory() as session:
            return session.scalar(
                select(StrategyDailyResultRow)
                .where(
                    StrategyDailyResultRow.run_id == run_id,
                    StrategyDailyResultRow.arm_id == arm_id,
                    StrategyDailyResultRow.session_date < before,
                )
                .order_by(StrategyDailyResultRow.session_date.desc())
                .limit(1)
            )

    def _session_economics(
        self,
        *,
        run_id: str,
        arm_id: str,
        calendar: VersionedMarketSession,
        previous_nav: Decimal,
    ) -> dict[str, Decimal]:
        with self._session_factory() as session:
            rows = tuple(
                session.scalars(
                    select(FillRow).where(
                        FillRow.run_id == run_id,
                        FillRow.arm_id == arm_id,
                        FillRow.algorithm_version == "q1_math_core_v1",
                        FillRow.effective_at >= calendar.open_at,
                        FillRow.effective_at < calendar.close_at,
                    )
                )
            )
            quote_ids = tuple(
                row.quote_id
                for row in rows
                if row.quote_id is not None
            )
            quote_rows = (
                ()
                if not quote_ids
                else tuple(
                    session.scalars(
                        select(MarketQuoteRow).where(
                            MarketQuoteRow.quote_id.in_(quote_ids)
                        )
                    )
                )
            )
        quote_by_id = {row.quote_id: row for row in quote_rows}
        asset_value_deltas: dict[str, Decimal] = {}
        cash_value_delta = Decimal("0")
        commissions = Decimal("0")
        spread = Decimal("0")
        delay = Decimal("0")
        sensitivity_5 = Decimal("0")
        sensitivity_10 = Decimal("0")
        for row in rows:
            if (
                row.quantity is None
                or row.price is None
                or row.commission_usd is None
                or row.quote_id is None
                or row.side is None
            ):
                raise Q1EvaluationCycleError(
                    "Q1 fill lacks typed evaluation fields"
                )
            quote = quote_by_id.get(row.quote_id)
            if quote is None:
                raise Q1EvaluationCycleError(
                    "Q1 fill quote is missing during evaluation"
                )
            quantity = Decimal(row.quantity)
            price = Decimal(row.price)
            notional = quantity * price
            direction = (
                Decimal("1")
                if row.side == OrderSide.BUY.value
                else Decimal("-1")
            )
            symbol = row.symbol
            if symbol is None:
                raise Q1EvaluationCycleError(
                    "Q1 fill symbol is missing during evaluation"
                )
            asset_value_deltas[symbol] = (
                asset_value_deltas.get(symbol, Decimal("0"))
                + direction * notional
            )
            cash_value_delta -= direction * notional
            commissions += Decimal(row.commission_usd)
            midpoint = (
                quote.bid_price + quote.ask_price
            ) / Decimal("2")
            if row.side == OrderSide.BUY.value:
                spread += (quote.ask_price - midpoint) * quantity
                delay += (price - quote.ask_price) * quantity
            else:
                spread += (midpoint - quote.bid_price) * quantity
                delay += (quote.bid_price - price) * quantity
            sensitivity_5 += Decimal(
                row.sensitivity_5bp_cost_usd or 0
            )
            sensitivity_10 += Decimal(
                row.sensitivity_10bp_cost_usd or 0
            )
        return {
            "turnover": (
                Decimal("0")
                if previous_nav <= 0
                else (
                    Decimal("0.5")
                    * (
                        sum(
                            (
                                abs(delta)
                                for delta in asset_value_deltas.values()
                            ),
                            Decimal("0"),
                        )
                        + abs(cash_value_delta)
                    )
                    / previous_nav
                )
            ),
            "commissions": commissions,
            "spread": max(Decimal("0"), spread),
            "delay": max(Decimal("0"), delay),
            "sensitivity_5bp": sensitivity_5,
            "sensitivity_10bp": sensitivity_10,
        }

    def _risk_episode_count(
        self,
        *,
        run_id: str,
        arm_id: str,
        calendar: VersionedMarketSession,
    ) -> int:
        with self._session_factory() as session:
            episodes = tuple(
                session.scalars(
                    select(RiskEpisodeRow).where(
                        RiskEpisodeRow.run_id == run_id,
                        RiskEpisodeRow.arm_id == arm_id,
                        RiskEpisodeRow.triggered_at < calendar.close_at,
                    )
                )
            )
            if not episodes:
                return 0
            release_rows = tuple(
                session.scalars(
                    select(RiskEpisodeEventRow).where(
                        RiskEpisodeEventRow.risk_episode_id.in_(
                            [row.risk_episode_id for row in episodes]
                        ),
                        RiskEpisodeEventRow.event_type == "RELEASE",
                        RiskEpisodeEventRow.occurred_at
                        <= calendar.open_at,
                    )
                )
            )
        released_before_open = {
            row.risk_episode_id
            for row in release_rows
        }
        return sum(
            row.risk_episode_id not in released_before_open
            for row in episodes
        )

    def _llm_reduction_count(
        self,
        *,
        run_id: str,
        arm_id: Q1ArmId,
        calendar: VersionedMarketSession,
    ) -> int:
        if arm_id is not Q1ArmId.Q1_LLM:
            return 0
        with self._session_factory() as session:
            rows = tuple(
                session.scalars(
                    select(PortfolioDecisionRow).where(
                        PortfolioDecisionRow.run_id == run_id,
                        PortfolioDecisionRow.arm_id
                        == Q1ArmId.Q1_LLM.value,
                        PortfolioDecisionRow.algorithm_version
                        == "q1_math_core_v1",
                        PortfolioDecisionRow.decision_created_at
                        >= calendar.open_at,
                        PortfolioDecisionRow.decision_created_at
                        < calendar.close_at,
                    )
                )
            )
        return sum(
            Q1StrategyDecision.model_validate(
                row.payload_json
            ).diagnostics.get("llm_overlay_state")
            not in {None, "NO_CHANGE", "NOT_APPLICABLE"}
            for row in rows
        )

    def _observations_with_prepared(
        self,
        *,
        run_id: str,
        prepared: dict[str, _DailyPrepared],
    ) -> dict[Q1ArmId, tuple[DailyEvaluationObservation, ...]]:
        result: dict[Q1ArmId, tuple[DailyEvaluationObservation, ...]] = {}
        with self._session_factory() as session:
            repository = StrategyEvaluationResultRepository(session)
            for arm_id in Q1ArmId:
                rows = repository.daily_results(
                    run_id=run_id,
                    arm_id=arm_id.value,
                )
                observations = [
                    _observation_from_row(row)
                    for row in rows
                ]
                current = prepared.get(arm_id.value)
                if current is not None:
                    observations.append(current.observation)
                if observations:
                    result[arm_id] = tuple(observations)
        return result

    def _prepare_matched(
        self,
        *,
        cycle: PaperCycleRow,
        calendar: VersionedMarketSession,
        anchor: StrategyEvaluationAnchor,
        observations: dict[
            Q1ArmId,
            tuple[DailyEvaluationObservation, ...],
        ],
        now: datetime,
        code_version: str,
        source_manifest_hash: str,
    ) -> tuple[MatchedAttributionResult, ...]:
        config = evaluation_config(self._runtime.config)
        result: list[MatchedAttributionResult] = []
        for comparison, left, right in (
            (
                MatchedComparison.Q1_DET_MINUS_B0_VOL,
                Q1ArmId.Q1_DET,
                Q1ArmId.B0_VOL,
            ),
            (
                MatchedComparison.Q1_LLM_MINUS_Q1_DET,
                Q1ArmId.Q1_LLM,
                Q1ArmId.Q1_DET,
            ),
        ):
            if left not in observations or right not in observations:
                continue
            matched = evaluate_matched_attribution(
                comparison=comparison,
                left_observations=observations[left],
                right_observations=observations[right],
                config=config,
            )
            result.append(
                _matched_record(
                    matched,
                    cycle=cycle,
                    calendar=calendar,
                    anchor=anchor,
                    now=now,
                    config_manifest_hash=(
                        self._runtime.config.manifest_hash
                    ),
                    code_version=code_version,
                    source_manifest_hash=source_manifest_hash,
                    newey_west_lag=config.newey_west_lag,
                    bootstrap_seed=config.bootstrap_seed,
                )
            )
        return tuple(result)

    def _expiry_events(
        self,
        *,
        cycle: PaperCycleRow,
        book: Q1OrderBook,
        now: datetime,
        source_manifest_hash: str,
    ) -> tuple[OrderEvent, ...]:
        provenance = OrderEventProvenance(
            config_manifest_hash=self._runtime.config.manifest_hash,
            code_version=workspace_code_version(self._workspace_root),
            model_version=Q1_MODEL_VERSION,
            source_manifest_hash=source_manifest_hash,
            worker_fence_token=_lease_owner(cycle),
            cycle_attempt_count=cycle.attempt_count,
        )
        provisional = expire_orders(
            orders=book.descriptors,
            events=book.events,
            as_of=now,
            provenance=provenance,
            source_cycle_id=cycle.cycle_id,
            expire_at_boundary=True,
        )
        intent_by_id = {
            intent.order_intent_id: intent
            for intent in book.intents
        }
        return tuple(
            append_order_event(
                order=next(
                    descriptor
                    for descriptor in book.descriptors
                    if descriptor.order_intent_id
                    == event.order_intent_id
                ),
                existing_events=book.events,
                event_type=event.event_type,
                occurred_at=now,
                available_at=now,
                provenance=provenance,
                reason="ACTUAL_SESSION_CLOSE_REACHED",
                source_cycle_id=cycle.cycle_id,
            )
            for event in provisional
            if event.order_intent_id in intent_by_id
        )

    def _commit(
        self,
        cycle: PaperCycleRow,
        *,
        calendar: VersionedMarketSession,
        anchor: StrategyEvaluationAnchor,
        prepared: dict[str, _DailyPrepared],
        matched: tuple[MatchedAttributionResult, ...],
        performance: dict[Q1ArmId, PerformanceMetrics],
        expiry_events: tuple[OrderEvent, ...],
        states: dict[str, Q1ArmState],
        now: datetime,
        source_manifest_hash: str,
        unavailable_arms: dict[str, str],
    ) -> dict[str, object]:
        with self._session_factory.begin() as session:
            locked = require_cycle_fence(
                session,
                cycle_id=cycle.cycle_id,
                lease_owner=_lease_owner(cycle),
                attempt_count=cycle.attempt_count,
                fallback_now=now,
            )
            for arm_id, expected in states.items():
                actual = latest_arm_state(
                    session,
                    run_id=cycle.run_id,
                    arm_id=arm_id,
                    lock=True,
                )
                if actual is None or actual.sequence != expected.sequence:
                    raise Q1EvaluationCycleError(
                        f"{arm_id} changed during daily evaluation"
                    )
            result_repository = StrategyEvaluationResultRepository(session)
            for item in prepared.values():
                result_repository.append_daily(item.result)
            for result in matched:
                result_repository.append_matched(result)
            order_repository = OrderEventRepository(session)
            for event in expiry_events:
                order_repository.append(event)
            output: dict[str, object] = {
                "status": "Q1_DAILY_RESULTS_COMMITTED",
                "evaluation_anchor_id": anchor.evaluation_anchor_id,
                "daily_result_ids": [
                    item.result.strategy_daily_result_id
                    for item in prepared.values()
                ],
                "matched_result_ids": [
                    item.matched_attribution_result_id
                    for item in matched
                ],
                "expired_order_event_ids": [
                    event.event_id for event in expiry_events
                ],
                "performance": {
                    arm_id.value: canonical_data(asdict(metrics))
                    for arm_id, metrics in performance.items()
                },
                "matched_readiness": {
                    item.comparison.value: {
                        "common_valid_sessions": (
                            item.common_valid_sessions
                        ),
                        "promotion_ready": item.promotion_ready,
                        "promotion_is_manual": True,
                    }
                    for item in matched
                },
                "unavailable_separate_reporting_arms": unavailable_arms,
                "real_order_routing": False,
            }
            complete_fenced_cycle(
                locked,
                cutoff=now,
                input_manifest={
                    "cycle_id": cycle.cycle_id,
                    "calendar_session_id": calendar.calendar_session_id,
                    "valuation_at": calendar.close_at,
                    "source_manifest_hash": source_manifest_hash,
                    "unavailable_separate_reporting_arms": (
                        unavailable_arms
                    ),
                    "config_manifest_hash": (
                        self._runtime.config.manifest_hash
                    ),
                    "real_order_routing": False,
                },
                output_manifest=output,
                completed_at=now,
            )
            return output


def _evaluation_weights(
    state: Q1ArmState,
    prices: dict[str, Decimal],
) -> dict[str, Decimal]:
    nav = state.nav(prices)
    weights = {
        symbol: quantity * prices[symbol] / nav
        for symbol, quantity in state.positions.items()
        if quantity > 0
    }
    weights["USD_CASH"] = state.total_cash_usd / nav
    return weights


def _observation_from_row(
    row: StrategyDailyResultRow,
) -> DailyEvaluationObservation:
    return DailyEvaluationObservation(
        session_date=row.session_date,
        arm_id=Q1ArmId(row.arm_id),
        net_daily_return=row.net_daily_return,
        daily_turnover=row.daily_turnover,
        commissions_usd=row.commissions_usd,
        spread_cost_usd=row.spread_cost_usd,
        delay_cost_usd=row.delay_cost_usd,
        sensitivity_5bp_usd=row.sensitivity_5bp_usd,
        sensitivity_10bp_usd=row.sensitivity_10bp_usd,
        cash_weight=row.cash_weight,
        qqq_weight=row.qqq_weight,
        soxx_weight=row.soxx_weight,
        risk_episode_active=row.active_risk_episode_count > 0,
        llm_reduction_active=row.active_llm_reduction_count > 0,
    )


def _matched_record(
    matched: MatchedAttribution,
    *,
    cycle: PaperCycleRow,
    calendar: VersionedMarketSession,
    anchor: StrategyEvaluationAnchor,
    now: datetime,
    config_manifest_hash: str,
    code_version: str,
    source_manifest_hash: str,
    newey_west_lag: int,
    bootstrap_seed: int,
) -> MatchedAttributionResult:
    values: dict[str, object] = {
        "evaluation_anchor_id": anchor.evaluation_anchor_id,
        "run_id": cycle.run_id,
        "comparison": matched.comparison,
        "left_arm_id": matched.left_arm_id,
        "right_arm_id": matched.right_arm_id,
        "through_session_date": calendar.session_date,
        "common_valid_sessions": matched.common_valid_sessions,
        "mean_daily_difference": matched.mean_daily_difference,
        "annualized_difference": matched.annualized_difference,
        "newey_west_lag": newey_west_lag,
        "newey_west_standard_error": (
            matched.newey_west_standard_error
        ),
        "bootstrap_seed": bootstrap_seed,
        "bootstrap_lower": matched.bootstrap_lower,
        "bootstrap_upper": matched.bootstrap_upper,
        "promotion_ready": (
            matched.eligible_for_manual_promotion_review
        ),
        "algorithm_version": "q1_math_core_v1",
        "config_manifest_hash": config_manifest_hash,
        "code_version": code_version,
        "model_version": Q1_MODEL_VERSION,
        "source_manifest_hash": source_manifest_hash,
        "created_at": now,
    }
    result_hash = canonical_hash(values)
    return MatchedAttributionResult(
        matched_attribution_result_id=stable_id(
            "q1-matched-result",
            cycle.run_id,
            matched.comparison,
            calendar.session_date,
            result_hash,
        ),
        evaluation_anchor_id=anchor.evaluation_anchor_id,
        run_id=cycle.run_id,
        comparison=matched.comparison,
        left_arm_id=matched.left_arm_id,
        right_arm_id=matched.right_arm_id,
        through_session_date=calendar.session_date,
        common_valid_sessions=matched.common_valid_sessions,
        mean_daily_difference=matched.mean_daily_difference,
        annualized_difference=matched.annualized_difference,
        newey_west_lag=newey_west_lag,
        newey_west_standard_error=matched.newey_west_standard_error,
        bootstrap_seed=bootstrap_seed,
        bootstrap_lower=matched.bootstrap_lower,
        bootstrap_upper=matched.bootstrap_upper,
        promotion_ready=matched.eligible_for_manual_promotion_review,
        algorithm_version="q1_math_core_v1",
        config_manifest_hash=config_manifest_hash,
        code_version=code_version,
        model_version=Q1_MODEL_VERSION,
        source_manifest_hash=source_manifest_hash,
        result_hash=result_hash,
        created_at=now,
    )


def _lease_owner(cycle: PaperCycleRow) -> str:
    if cycle.lease_owner is None:
        raise Q1EvaluationCycleError("Q1 evaluation cycle has no lease owner")
    return cycle.lease_owner


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
