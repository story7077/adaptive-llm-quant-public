from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, time
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trading.data.alpaca import FEED, PROVIDER
from trading.data.market_repository import MarketDataRepository
from trading.domain.algorithm import Q1_ALGORITHM_VERSION
from trading.domain.contracts import NewsEvent, model_payload
from trading.domain.enums import MarketConnectionState, OrderSide
from trading.domain.hashing import canonical_hash, stable_id
from trading.domain.q1 import (
    OrderEvent,
    OrderEventType,
    PointInTimeSourceReference,
    Q1ArmId,
    Q1DecisionInputManifest,
    Q1StrategyDecision,
)
from trading.domain.q1_runtime import Q1Fill, Q1OrderIntent
from trading.domain.time import Clock, SystemClock, require_aware_utc
from trading.execution.order_state import (
    OrderDescriptor,
    OrderEventProvenance,
    Q1OrderClass,
    append_order_event,
    pending_orders,
)
from trading.llm.q1_overlay import (
    Q1LlmOverlayDecision,
    Q1OverlayState,
    validate_bounded_evidence,
)
from trading.persistence.models import (
    ArmStateSnapshotRow,
    FillRow,
    NewsEventRow,
    OrderIntentRow,
    PaperCycleRow,
)
from trading.persistence.q1 import (
    OrderEventRepository,
    Q1StrategyDecisionRepository,
    RiskEpisodeRepository,
)
from trading.persistence.q1_runtime import (
    Q1OrderBook,
    append_order_intent,
    append_risk_approval,
    append_strategy_decision,
    complete_fenced_cycle,
    latest_arm_state,
    load_q1_order_book,
    require_cycle_fence,
)
from trading.quant.allocator import TurnoverResult, apply_turnover_control
from trading.runtime.provenance import workspace_code_version
from trading.runtime.q1_config import (
    maximum_quote_age_seconds,
    maximum_quote_skew_seconds,
    order_planning_config,
)
from trading.runtime.q1_paper import Q1PaperRuntimeService
from trading.runtime.q1_planning import (
    DecisionQuote,
    PlannedOrders,
    build_portfolio_decision,
    plan_target_quantity_sell_orders,
    risk_approval_id,
)
from trading.runtime.q1_scheduler import VersionedMarketSession
from trading.runtime.q1_state import Q1ArmState

Q1_LLM_REVIEW_MODEL_VERSION = "q1_llm_callback_v1"
RISKY_SYMBOLS = ("QQQ", "SOXX")
CASH_SYMBOL = "USD_CASH"

LlmReviewProvider = Callable[
    [dict[str, Any]],
    Q1LlmOverlayDecision | Mapping[str, object] | None,
]
Q1NewsRefresher = Callable[[PaperCycleRow, datetime], object]


class Q1LlmReviewError(RuntimeError):
    pass


class _ReviewUnavailable(Q1LlmReviewError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class _PreparedReview:
    calendar: VersionedMarketSession
    state: Q1ArmState
    state_as_of: datetime
    deterministic_decision: Q1StrategyDecision
    previous_llm_decision: Q1StrategyDecision | None
    current_nav_usd: Decimal
    current_weights: dict[str, Decimal]
    quotes: dict[str, DecisionQuote]
    news_events: tuple[NewsEvent, ...]
    input_manifest: Q1DecisionInputManifest
    provider_request: dict[str, Any]
    order_book: Q1OrderBook
    order_book_hash: str
    deterministic_risk_active: bool
    used_normal_turnover: Decimal
    turnover_fill_references: tuple[dict[str, object], ...]


@dataclass(frozen=True, slots=True)
class _PreparedCommit:
    decision: Q1StrategyDecision
    planned: PlannedOrders
    cancellations: tuple[OrderEvent, ...]
    state_sequence: int
    deterministic_decision_id: str
    order_book_hash: str
    overlay_state: Q1OverlayState
    policy_id: str | None


class Q1LlmReviewCycleProcessor:
    """Process the independent noon Q1-LLM reduce-only lane.

    The provider receives a bounded, hash-addressed request. It cannot create
    orders directly. A valid response is converted to a Q1-LLM target that is
    bounded by both the matched Q1-DET target and the current Q1-LLM exposure.
    All mutations and cycle completion share the claimed database fence.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        runtime: Q1PaperRuntimeService,
        workspace_root: Path,
        llm_overlay_provider: LlmReviewProvider | None,
        news_refresher: Q1NewsRefresher | None = None,
        clock: Clock | None = None,
        code_version: str | None = None,
        model_version: str = Q1_LLM_REVIEW_MODEL_VERSION,
    ) -> None:
        if not model_version.strip():
            raise ValueError("Q1 LLM review model_version is required")
        self._session_factory = session_factory
        self._runtime = runtime
        self._workspace_root = workspace_root
        self._provider = llm_overlay_provider
        self._news_refresher = news_refresher
        self._clock = clock or SystemClock()
        self._market = MarketDataRepository(session_factory)
        self._code_version = code_version or workspace_code_version(workspace_root)
        self._model_version = model_version
        llm = _mapping(runtime.config.document, "llm")
        self._maximum_evidence_events = _positive_int(
            llm,
            "maximum_evidence_events",
        )
        self._provider_timeout_seconds = _positive_decimal(
            llm,
            "provider_timeout_seconds",
        )
        self._no_new_policy_after = _parse_clock(
            str(llm["no_new_policy_after_et"])
        )

    def process(self, cycle: PaperCycleRow) -> dict[str, Any]:
        if cycle.cycle_kind != "Q1_LLM_REVIEW":
            raise Q1LlmReviewError(
                f"Unsupported Q1 LLM review cycle {cycle.cycle_kind!r}"
            )
        if not cycle.lease_owner:
            raise Q1LlmReviewError("Claimed Q1 LLM review has no lease owner")

        now = require_aware_utc(self._clock.now())
        scheduled_at = _aware(cycle.scheduled_at)
        if self._news_refresher is not None:
            with suppress(Exception):
                self._news_refresher(cycle, scheduled_at)
        try:
            prepared = self._prepare(
                cycle=cycle,
                scheduled_at=scheduled_at,
                now=now,
            )
        except _ReviewUnavailable as exc:
            return self._complete_no_change(
                cycle,
                now=now,
                scheduled_at=scheduled_at,
                reason=exc.code,
            )
        except Exception:
            return self._complete_no_change(
                cycle,
                now=now,
                scheduled_at=scheduled_at,
                reason="INVALID_REVIEW_CONTEXT",
            )

        expired_policy_id = _expired_policy_id(
            prepared.previous_llm_decision,
            now=now,
        )
        if expired_policy_id is not None:
            commit = self._prepare_commit(
                cycle=cycle,
                prepared=prepared,
                policy=None,
                overlay_state=Q1OverlayState.EXPIRED_AWAITING_NEXT_REBALANCE,
                policy_id=expired_policy_id,
                now=now,
            )
            return self._commit(cycle, prepared=prepared, commit=commit, now=now)

        if prepared.deterministic_risk_active:
            return self._complete_no_change(
                cycle,
                now=now,
                scheduled_at=scheduled_at,
                reason="DETERMINISTIC_RISK_PRECEDENCE",
                context_manifest_hash=(
                    prepared.provider_request["context_manifest_hash"]
                ),
            )
        if not _inside_regular_session(now, prepared.calendar):
            return self._complete_no_change(
                cycle,
                now=now,
                scheduled_at=scheduled_at,
                reason="OUTSIDE_REGULAR_SESSION",
                context_manifest_hash=(
                    prepared.provider_request["context_manifest_hash"]
                ),
            )
        policy_cutoff = _session_clock(
            prepared.calendar,
            self._no_new_policy_after,
            timezone=self._runtime.schedule_timezone,
        )
        if now >= policy_cutoff:
            return self._complete_no_change(
                cycle,
                now=now,
                scheduled_at=scheduled_at,
                reason="NEW_POLICY_CUTOFF_REACHED",
                context_manifest_hash=(
                    prepared.provider_request["context_manifest_hash"]
                ),
            )

        policy, provider_error = self._invoke_provider(
            prepared.provider_request
        )
        provider_audit = _provider_audit_payload(
            self._provider,
            str(prepared.provider_request["request_id"]),
        )
        now = require_aware_utc(self._clock.now())
        if policy is None:
            return self._complete_no_change(
                cycle,
                now=now,
                scheduled_at=scheduled_at,
                reason=provider_error or "PROVIDER_NO_CHANGE",
                context_manifest_hash=(
                    prepared.provider_request["context_manifest_hash"]
                ),
                provider_audit=provider_audit,
            )
        if (
            not _inside_regular_session(now, prepared.calendar)
            or now >= policy_cutoff
        ):
            return self._complete_no_change(
                cycle,
                now=now,
                scheduled_at=scheduled_at,
                reason=(
                    "SESSION_CLOSED_DURING_PROVIDER_CALL"
                    if not _inside_regular_session(now, prepared.calendar)
                    else "NEW_POLICY_CUTOFF_REACHED_DURING_PROVIDER_CALL"
                ),
                context_manifest_hash=(
                    prepared.provider_request["context_manifest_hash"]
                ),
                provider_audit=provider_audit,
            )
        try:
            self._validate_policy(
                policy,
                prepared=prepared,
                now=now,
                policy_cutoff=policy_cutoff,
            )
        except (ValueError, TypeError):
            return self._complete_no_change(
                cycle,
                now=now,
                scheduled_at=scheduled_at,
                reason="INVALID_PROVIDER_OUTPUT",
                context_manifest_hash=(
                    prepared.provider_request["context_manifest_hash"]
                ),
                provider_audit=provider_audit,
            )

        overlay_state = (
            Q1OverlayState.EXPIRED_AWAITING_NEXT_REBALANCE
            if now >= policy.expiry_time
            else Q1OverlayState.ACTIVE
        )
        commit = self._prepare_commit(
            cycle=cycle,
            prepared=prepared,
            policy=policy,
            overlay_state=overlay_state,
            policy_id=policy.request_id,
            now=now,
            provider_audit=provider_audit,
        )
        return self._commit(cycle, prepared=prepared, commit=commit, now=now)

    def _prepare(
        self,
        *,
        cycle: PaperCycleRow,
        scheduled_at: datetime,
        now: datetime,
    ) -> _PreparedReview:
        if now < scheduled_at:
            raise _ReviewUnavailable("REVIEW_NOT_DUE")
        session_date = scheduled_at.astimezone(
            self._runtime.schedule_timezone
        ).date()
        calendar = self._runtime.calendar_session(
            session_date=session_date,
            cutoff=scheduled_at,
        )
        if calendar is None:
            raise _ReviewUnavailable("VERSIONED_CALENDAR_UNAVAILABLE")
        if now >= calendar.close_at:
            raise _ReviewUnavailable("SESSION_CLOSED")

        with self._session_factory() as session:
            state_row = session.scalar(
                select(ArmStateSnapshotRow)
                .where(
                    ArmStateSnapshotRow.run_id == cycle.run_id,
                    ArmStateSnapshotRow.arm_id == Q1ArmId.Q1_LLM.value,
                )
                .order_by(
                    ArmStateSnapshotRow.sequence.desc(),
                    ArmStateSnapshotRow.arm_state_snapshot_id.desc(),
                )
                .limit(1)
            )
            if state_row is None:
                raise _ReviewUnavailable("Q1_LLM_STATE_UNAVAILABLE")
            state = Q1ArmState.from_payload(state_row.payload_json)
            decision_repository = Q1StrategyDecisionRepository(session)
            deterministic_row = decision_repository.latest_as_of(
                run_id=cycle.run_id,
                arm_id=Q1ArmId.Q1_DET.value,
                as_of=now,
            )
            if deterministic_row is None:
                raise _ReviewUnavailable("Q1_DET_TARGET_UNAVAILABLE")
            deterministic_decision = Q1StrategyDecision.model_validate(
                deterministic_row.payload_json
            )
            previous_llm_row = decision_repository.latest_as_of(
                run_id=cycle.run_id,
                arm_id=Q1ArmId.Q1_LLM.value,
                as_of=now,
            )
            previous_llm_decision = (
                None
                if previous_llm_row is None
                else Q1StrategyDecision.model_validate(
                    previous_llm_row.payload_json
                )
            )
            news_events = self._bounded_news_events(
                session,
                cutoff=scheduled_at,
                available_by=now,
            )
            risk_active = (
                RiskEpisodeRepository(session).active(
                    run_id=cycle.run_id,
                    arm_id=Q1ArmId.Q1_LLM.value,
                )
                is not None
            )
            order_book = load_q1_order_book(
                session,
                run_id=cycle.run_id,
                arm_id=Q1ArmId.Q1_LLM.value,
            )
            turnover_rows = tuple(
                session.execute(
                    select(FillRow, OrderIntentRow)
                    .join(
                        OrderIntentRow,
                        OrderIntentRow.order_intent_id
                        == FillRow.order_intent_id,
                    )
                    .where(
                        FillRow.run_id == cycle.run_id,
                        FillRow.arm_id == Q1ArmId.Q1_LLM.value,
                        FillRow.algorithm_version
                        == Q1_ALGORITHM_VERSION,
                        FillRow.execution_scenario_id
                        == "Q1_BASE_V1",
                        FillRow.effective_at >= calendar.open_at,
                        FillRow.effective_at <= now,
                    )
                    .order_by(
                        FillRow.effective_at,
                        FillRow.fill_id,
                    )
                ).tuples()
            )

        quotes = self._fresh_quotes(now)
        prices = {
            symbol: quote.midpoint
            for symbol, quote in quotes.items()
        }
        unknown_positions = sorted(
            set(state.positions) - set(RISKY_SYMBOLS)
        )
        if unknown_positions:
            raise _ReviewUnavailable("Q1_LLM_UNIVERSE_VIOLATION")
        current_nav = state.nav(prices)
        if current_nav <= 0:
            raise _ReviewUnavailable("Q1_LLM_NAV_INVALID")
        current_weights = _current_weights(state, prices)
        (
            used_normal_turnover,
            turnover_fill_references,
        ) = _used_normal_turnover(
            turnover_rows,
            current_nav_usd=current_nav,
            as_of=now,
        )
        deterministic_target = _normalized_target(
            deterministic_decision.target_weights
        )
        quote_refs = tuple(
            PointInTimeSourceReference(
                record_id=quotes[symbol].quote_id,
                available_at=quotes[symbol].available_at,
            )
            for symbol in RISKY_SYMBOLS
        )
        source_manifest_hash = canonical_hash(
            {
                "calendar_session_id": calendar.calendar_session_id,
                "deterministic_decision_id": (
                    deterministic_decision.portfolio_decision_id
                ),
                "deterministic_decision_hash": (
                    deterministic_decision.decision_hash
                ),
                "deterministic_source_manifest_hash": (
                    deterministic_decision.source_manifest_hash
                ),
                "state": state.as_payload(),
                "state_as_of": _aware(state_row.created_at),
                "quotes": quote_refs,
                "news_events": [
                    {
                        "news_event_id": event.news_event_id,
                        "output_hash": event.output_hash,
                    }
                    for event in news_events
                ],
                "used_normal_turnover": used_normal_turnover,
                "turnover_fill_references": turnover_fill_references,
            }
        )
        manifest_content = {
            "calendar_session_id": calendar.calendar_session_id,
            "source_bars": deterministic_decision.input_manifest.source_bars,
            "quotes": quote_refs,
            "config_manifest_hash": self._runtime.config.manifest_hash,
            "code_version": self._code_version,
            "model_version": self._model_version,
            "source_manifest_hash": source_manifest_hash,
        }
        input_manifest = Q1DecisionInputManifest(
            calendar_session_id=calendar.calendar_session_id,
            source_bars=deterministic_decision.input_manifest.source_bars,
            quotes=quote_refs,
            config_manifest_hash=self._runtime.config.manifest_hash,
            code_version=self._code_version,
            model_version=self._model_version,
            source_manifest_hash=source_manifest_hash,
            manifest_hash=canonical_hash(manifest_content),
        )
        context = {
            "calendar_session_id": calendar.calendar_session_id,
            "scheduled_at": scheduled_at,
            "portfolio_state_as_of": _aware(state_row.created_at),
            "quote_as_of": max(
                quote.available_at
                for quote in quotes.values()
            ),
            "q1_det": {
                "portfolio_decision_id": (
                    deterministic_decision.portfolio_decision_id
                ),
                "decision_hash": deterministic_decision.decision_hash,
                "target_weights": deterministic_target,
                "input_manifest_hash": (
                    deterministic_decision.input_manifest.manifest_hash
                ),
            },
            "q1_llm": {
                "state_sequence": state.sequence,
                "positions": state.positions,
                "settled_cash_usd": state.settled_cash_usd,
                "unsettled_receivables_usd": state.unsettled_cash_usd,
                "current_nav_usd": current_nav,
                "current_weights": current_weights,
                "used_normal_turnover": used_normal_turnover,
                "turnover_fill_references": turnover_fill_references,
            },
            "quotes": {
                symbol: {
                    "quote_id": quote.quote_id,
                    "bid": quote.bid,
                    "ask": quote.ask,
                    "available_at": quote.available_at,
                }
                for symbol, quote in quotes.items()
            },
            "news_events": [
                model_payload(event)
                for event in news_events
            ],
            "allowed_evidence_event_ids": [
                event.news_event_id
                for event in news_events
            ],
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
            cycle.run_id,
            scheduled_at,
            context_manifest_hash,
        )
        provider_request: dict[str, Any] = {
            "schema_version": "q1_llm_review_request_v1",
            "request_id": request_id,
            "context_manifest_hash": context_manifest_hash,
            **context,
        }
        return _PreparedReview(
            calendar=calendar,
            state=state,
            state_as_of=_aware(state_row.created_at),
            deterministic_decision=deterministic_decision,
            previous_llm_decision=previous_llm_decision,
            current_nav_usd=current_nav,
            current_weights=current_weights,
            quotes=quotes,
            news_events=news_events,
            input_manifest=input_manifest,
            provider_request=provider_request,
            order_book=order_book,
            order_book_hash=_order_book_hash(order_book),
            deterministic_risk_active=risk_active,
            used_normal_turnover=used_normal_turnover,
            turnover_fill_references=turnover_fill_references,
        )

    def _bounded_news_events(
        self,
        session: Session,
        *,
        cutoff: datetime,
        available_by: datetime,
    ) -> tuple[NewsEvent, ...]:
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
            if len(valid) == self._maximum_evidence_events:
                break
        return tuple(valid)

    def _fresh_quotes(
        self,
        now: datetime,
    ) -> dict[str, DecisionQuote]:
        status = self._market.status(provider=PROVIDER, feed=FEED)
        if (
            status is None
            or status.state != MarketConnectionState.CONNECTED.value
        ):
            raise _ReviewUnavailable("MARKET_STREAM_DISCONNECTED")
        rows: dict[str, DecisionQuote] = {}
        event_times: list[datetime] = []
        for symbol in RISKY_SYMBOLS:
            row = self._market.latest_quote(
                provider=PROVIDER,
                feed=FEED,
                symbol=symbol,
                as_of=now,
            )
            if row is None:
                raise _ReviewUnavailable("FRESH_QUOTE_UNAVAILABLE")
            event_time = _aware(row.event_time)
            available_at = _aware(row.available_at)
            age_seconds = (now - event_time).total_seconds()
            if (
                age_seconds < 0
                or age_seconds
                > maximum_quote_age_seconds(self._runtime.config)
                or available_at > now
                or row.bid_price <= 0
                or row.ask_price <= 0
                or row.ask_price < row.bid_price
                or row.bid_size_round_lots <= 0
            ):
                raise _ReviewUnavailable("FRESH_QUOTE_INVALID")
            rows[symbol] = DecisionQuote(
                symbol=symbol,
                quote_id=row.quote_id,
                bid=row.bid_price,
                ask=row.ask_price,
                available_at=available_at,
            )
            event_times.append(event_time)
        if (
            max(event_times) - min(event_times)
        ).total_seconds() > maximum_quote_skew_seconds(
            self._runtime.config
        ):
            raise _ReviewUnavailable("QUOTE_BUNDLE_SKEW")
        return rows

    def _invoke_provider(
        self,
        request: dict[str, Any],
    ) -> tuple[Q1LlmOverlayDecision | None, str | None]:
        if self._provider is None:
            return None, "PROVIDER_UNAVAILABLE"
        provider = self._provider
        result_queue: Queue[tuple[bool, object]] = Queue(maxsize=1)

        def invoke() -> None:
            try:
                result_queue.put((True, provider(request)))
            except Exception:
                result_queue.put((False, None))

        Thread(
            target=invoke,
            name="q1-llm-review-provider",
            daemon=True,
        ).start()
        try:
            succeeded, raw = result_queue.get(
                timeout=float(self._provider_timeout_seconds)
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
                payload = dict(cast(Mapping[str, object], raw))
                return (
                    Q1LlmOverlayDecision.model_validate(payload),
                    None,
                )
        except Exception:
            return None, "INVALID_PROVIDER_OUTPUT"
        return None, "INVALID_PROVIDER_OUTPUT"

    def _validate_policy(
        self,
        policy: Q1LlmOverlayDecision,
        *,
        prepared: _PreparedReview,
        now: datetime,
        policy_cutoff: datetime,
    ) -> None:
        request = prepared.provider_request
        if policy.request_id != request["request_id"]:
            raise ValueError("Provider response request_id mismatch")
        if policy.context_manifest_hash != request["context_manifest_hash"]:
            raise ValueError("Provider response context hash mismatch")
        validate_bounded_evidence(
            policy,
            allowed_event_ids={
                event.news_event_id
                for event in prepared.news_events
            },
        )
        if policy.created_at > now:
            raise ValueError("Provider response was created in the future")
        if policy.effective_time > now:
            raise ValueError("Future Q1 LLM policies are not scheduled")
        if policy.effective_time >= policy_cutoff:
            raise ValueError("Q1 LLM policy effective time is after cutoff")

    def _prepare_commit(
        self,
        *,
        cycle: PaperCycleRow,
        prepared: _PreparedReview,
        policy: Q1LlmOverlayDecision | None,
        overlay_state: Q1OverlayState,
        policy_id: str | None,
        now: datetime,
        provider_audit: dict[str, object] | None = None,
    ) -> _PreparedCommit:
        deterministic_target = _normalized_target(
            prepared.deterministic_decision.target_weights
        )
        turnover: TurnoverResult | None = None
        if overlay_state is Q1OverlayState.EXPIRED_AWAITING_NEXT_REBALANCE:
            final_target = dict(prepared.current_weights)
        else:
            if policy is None:
                raise Q1LlmReviewError("Active overlay requires a policy")
            multiplier = Decimal(str(policy.risk_multiplier))
            proposed_target = {
                symbol: min(
                    prepared.current_weights[symbol],
                    deterministic_target[symbol] * multiplier,
                )
                for symbol in RISKY_SYMBOLS
            }
            proposed_target[CASH_SYMBOL] = Decimal("1") - sum(
                (
                    proposed_target[symbol]
                    for symbol in RISKY_SYMBOLS
                ),
                Decimal("0"),
            )
            turnover = apply_turnover_control(
                current_weights=prepared.current_weights,
                proposed_target_weights=proposed_target,
                current_nav_usd=prepared.current_nav_usd,
                used_normal_turnover=prepared.used_normal_turnover,
                emergency_reduction=False,
                config=self._runtime.math_config,
            )
            final_target = dict(turnover.executable_target_weights)
        _assert_reduce_only(
            final_target,
            current=prepared.current_weights,
            deterministic=deterministic_target,
            enforce_deterministic_cap=(
                overlay_state is Q1OverlayState.ACTIVE
            ),
        )
        valid_until = prepared.calendar.close_at
        if policy is not None:
            valid_until = min(valid_until, policy.expiry_time)
        if valid_until <= now:
            valid_until = prepared.calendar.close_at
        input_manifest = self._augmented_input_manifest(
            prepared=prepared,
            policy=policy,
            provider_audit=provider_audit,
        )
        decision = build_portfolio_decision(
            run_id=cycle.run_id,
            arm_id=Q1ArmId.Q1_LLM,
            source_cycle_id=cycle.cycle_id,
            input_state_sequence=prepared.state.sequence,
            decision_kind=(
                "LLM_POLICY_EXPIRED"
                if overlay_state
                is Q1OverlayState.EXPIRED_AWAITING_NEXT_REBALANCE
                else "LLM_REDUCE_ONLY"
            ),
            scheduled_at=_aware(cycle.scheduled_at),
            signal_data_cutoff=_aware(cycle.scheduled_at),
            portfolio_state_as_of=prepared.state_as_of,
            quote_as_of=max(
                quote.available_at
                for quote in prepared.quotes.values()
            ),
            decision_created_at=now,
            valid_until=valid_until,
            current_weights=prepared.current_weights,
            deterministic_target_weights=deterministic_target,
            final_target_weights=final_target,
            expected_annualized_volatility=_expected_volatility(
                prepared.deterministic_decision,
                final_target,
            ),
            expected_one_way_turnover=_one_way_turnover(
                prepared.current_weights,
                final_target,
            ),
            used_daily_turnover_before=prepared.used_normal_turnover,
            signal_hash=_diagnostic_string(
                prepared.deterministic_decision,
                "signal_hash",
            ),
            allocation_hash=_diagnostic_string(
                prepared.deterministic_decision,
                "allocation_hash",
            ),
            llm_overlay_state=overlay_state.value,
            llm_policy_id=policy_id,
            diagnostics={
                "matched_q1_det_decision_id": (
                    prepared.deterministic_decision.portfolio_decision_id
                ),
                "matched_q1_det_decision_hash": (
                    prepared.deterministic_decision.decision_hash
                ),
                "llm_context_manifest_hash": (
                    prepared.provider_request["context_manifest_hash"]
                ),
                "llm_provider_audit": provider_audit or {},
                "llm_policy": (
                    {}
                    if policy is None
                    else model_payload(policy)
                ),
                "llm_evidence_event_ids": (
                    []
                    if policy is None
                    else list(policy.evidence_event_ids)
                ),
                "llm_risk_multiplier": (
                    None
                    if policy is None
                    else policy.risk_multiplier
                ),
                "llm_block_new_entries": (
                    None
                    if policy is None
                    else policy.block_new_entries
                ),
                "llm_rationale": (
                    "Policy expired; risk awaits the next strategic rebalance"
                    if policy is None
                    else policy.rationale
                ),
                "llm_policy_effective_time": (
                    None
                    if policy is None
                    else policy.effective_time
                ),
                "llm_policy_expiry_time": (
                    (
                        _previous_policy_expiry(
                            prepared.previous_llm_decision
                        )
                    )
                    if policy is None
                    else policy.expiry_time
                ),
                "normal_turnover": (
                    {
                        "decision_kind": turnover.decision_kind,
                        "proposed_one_way_turnover": (
                            turnover.proposed_one_way_turnover
                        ),
                        "remaining_daily_capacity": (
                            turnover.remaining_daily_capacity
                        ),
                        "interpolation_alpha": (
                            turnover.interpolation_alpha
                        ),
                        "executable_one_way_turnover": (
                            turnover.executable_one_way_turnover
                        ),
                        "omitted_orders": [
                            {
                                "symbol": item.symbol,
                                "side": item.side,
                                "proposed_notional_usd": (
                                    item.proposed_notional_usd
                                ),
                                "minimum_notional_usd": (
                                    item.minimum_notional_usd
                                ),
                                "reason": item.reason,
                            }
                            for item in turnover.omitted_orders
                        ],
                        "turnover_hash": turnover.turnover_hash,
                    }
                    if turnover is not None
                    else {
                        "decision_kind": "POLICY_EXPIRED_NO_RESTORE",
                        "proposed_one_way_turnover": Decimal("0"),
                        "remaining_daily_capacity": max(
                            Decimal("0"),
                            (
                                self._runtime.math_config.turnover
                                .normal_daily_one_way_cap
                                - prepared.used_normal_turnover
                            ),
                        ),
                        "interpolation_alpha": Decimal("0"),
                        "executable_one_way_turnover": Decimal("0"),
                        "omitted_orders": [],
                        "turnover_hash": canonical_hash(
                            {
                                "state": "POLICY_EXPIRED_NO_RESTORE",
                                "used_normal_turnover": (
                                    prepared.used_normal_turnover
                                ),
                            }
                        ),
                    }
                ),
                "turnover_fill_references": list(
                    prepared.turnover_fill_references
                ),
                "noon_reduce_only": True,
                "restoration_requires_next_strategic_decision": True,
                "real_order_routing": False,
            },
            input_manifest=input_manifest,
            worker_fence_token=_lease_owner(cycle),
            cycle_attempt_count=cycle.attempt_count,
        )

        provenance = self._event_provenance(
            cycle,
            input_manifest.source_manifest_hash,
        )
        cancellations = _buy_cancellations(
            prepared.order_book,
            occurred_at=now,
            provenance=provenance,
            source_cycle_id=cycle.cycle_id,
        )
        planned = PlannedOrders(
            intents=(),
            omitted=(),
            intent_manifest_hash=canonical_hash(
                {
                    "decision_id": decision.portfolio_decision_id,
                    "intents": [],
                    "omitted": [],
                }
            ),
        )
        if overlay_state is Q1OverlayState.ACTIVE:
            target_quantities = _executable_target_quantities(
                state=prepared.state,
                final_target=final_target,
                nav_usd=prepared.current_nav_usd,
                quotes=prepared.quotes,
                order_book=prepared.order_book,
                now=now,
                quantity_increment=order_planning_config(
                    self._runtime.config
                ).quantity_increment,
                minimum_notional=max(
                    self._runtime.math_config.turnover.minimum_order_notional_usd,
                    (
                        self._runtime.math_config.turnover
                        .minimum_order_nav_fraction
                        * prepared.current_nav_usd
                    ),
                ),
            )
            planned = plan_target_quantity_sell_orders(
                decision=decision,
                current_positions=prepared.state.positions,
                target_quantities=target_quantities,
                quotes=prepared.quotes,
                source_cycle_id=cycle.cycle_id,
                input_state_sequence=prepared.state.sequence,
                order_class=Q1OrderClass.LLM_REDUCTION,
                config=order_planning_config(self._runtime.config),
            )
        return _PreparedCommit(
            decision=decision,
            planned=planned,
            cancellations=cancellations,
            state_sequence=prepared.state.sequence,
            deterministic_decision_id=(
                prepared.deterministic_decision.portfolio_decision_id
            ),
            order_book_hash=prepared.order_book_hash,
            overlay_state=overlay_state,
            policy_id=policy_id,
        )

    def _augmented_input_manifest(
        self,
        *,
        prepared: _PreparedReview,
        policy: Q1LlmOverlayDecision | None,
        provider_audit: dict[str, object] | None,
    ) -> Q1DecisionInputManifest:
        audit = provider_audit or {}
        provider = audit.get("provider")
        model = audit.get("model")
        model_version = self._model_version
        if isinstance(provider, str) and isinstance(model, str):
            model_version = (
                f"{self._model_version}+commander:{provider}:{model}"
            )[:120]
        source_manifest_hash = canonical_hash(
            {
                "prepared_source_manifest_hash": (
                    prepared.input_manifest.source_manifest_hash
                ),
                "provider_request": prepared.provider_request,
                "policy": (
                    None
                    if policy is None
                    else model_payload(policy)
                ),
                "provider_audit": audit,
            }
        )
        content = {
            "calendar_session_id": (
                prepared.input_manifest.calendar_session_id
            ),
            "source_bars": prepared.input_manifest.source_bars,
            "quotes": prepared.input_manifest.quotes,
            "config_manifest_hash": (
                prepared.input_manifest.config_manifest_hash
            ),
            "code_version": prepared.input_manifest.code_version,
            "model_version": model_version,
            "source_manifest_hash": source_manifest_hash,
        }
        return Q1DecisionInputManifest(
            calendar_session_id=prepared.input_manifest.calendar_session_id,
            source_bars=prepared.input_manifest.source_bars,
            quotes=prepared.input_manifest.quotes,
            config_manifest_hash=(
                prepared.input_manifest.config_manifest_hash
            ),
            code_version=prepared.input_manifest.code_version,
            model_version=model_version,
            source_manifest_hash=source_manifest_hash,
            manifest_hash=canonical_hash(content),
        )

    def _event_provenance(
        self,
        cycle: PaperCycleRow,
        source_manifest_hash: str,
    ) -> OrderEventProvenance:
        return OrderEventProvenance(
            config_manifest_hash=self._runtime.config.manifest_hash,
            code_version=self._code_version,
            model_version=self._model_version,
            source_manifest_hash=source_manifest_hash,
            worker_fence_token=_lease_owner(cycle),
            cycle_attempt_count=cycle.attempt_count,
        )

    def _commit(
        self,
        cycle: PaperCycleRow,
        *,
        prepared: _PreparedReview,
        commit: _PreparedCommit,
        now: datetime,
    ) -> dict[str, Any]:
        cycle_input = {
            "cycle_id": cycle.cycle_id,
            "scheduled_at": _aware(cycle.scheduled_at),
            "calendar_session_id": prepared.calendar.calendar_session_id,
            "decision_input_manifest_hash": (
                commit.decision.input_manifest.manifest_hash
            ),
            "llm_provider_audit": (
                commit.decision.diagnostics.get(
                    "llm_provider_audit",
                    {},
                )
            ),
            "llm_context_manifest_hash": (
                prepared.provider_request["context_manifest_hash"]
            ),
            "deterministic_decision_id": commit.deterministic_decision_id,
            "state_sequence": commit.state_sequence,
            "order_book_hash": commit.order_book_hash,
            "config_manifest_hash": self._runtime.config.manifest_hash,
            "real_order_routing": False,
        }
        with self._session_factory.begin() as session:
            locked = require_cycle_fence(
                session,
                cycle_id=cycle.cycle_id,
                lease_owner=_lease_owner(cycle),
                attempt_count=cycle.attempt_count,
                fallback_now=now,
            )
            current_state = latest_arm_state(
                session,
                run_id=cycle.run_id,
                arm_id=Q1ArmId.Q1_LLM.value,
                lock=True,
            )
            latest_det = Q1StrategyDecisionRepository(session).latest_as_of(
                run_id=cycle.run_id,
                arm_id=Q1ArmId.Q1_DET.value,
                as_of=now,
            )
            current_book = load_q1_order_book(
                session,
                run_id=cycle.run_id,
                arm_id=Q1ArmId.Q1_LLM.value,
            )
            if (
                current_state is None
                or current_state.sequence != commit.state_sequence
                or latest_det is None
                or latest_det.portfolio_decision_id
                != commit.deterministic_decision_id
                or _order_book_hash(current_book) != commit.order_book_hash
            ):
                output = {
                    "status": "LLM_NO_CHANGE",
                    "reason": "REVIEW_INPUT_CHANGED_BEFORE_COMMIT",
                    "orders_created": 0,
                    "real_order_routing": False,
                }
            else:
                event_repository = OrderEventRepository(session)
                cancellation_ids: list[str] = []
                for event in commit.cancellations:
                    event_repository.append(event)
                    cancellation_ids.append(event.event_id)
                append_strategy_decision(session, decision=commit.decision)
                append_risk_approval(
                    session,
                    risk_decision_id=risk_approval_id(commit.decision),
                    decision=commit.decision,
                )
                intent_ids: list[str] = []
                provenance = self._event_provenance(
                    cycle,
                    commit.decision.source_manifest_hash,
                )
                for intent in commit.planned.intents:
                    append_order_intent(session, intent)
                    session.flush()
                    event_repository.append(
                        append_order_event(
                            order=_descriptor(intent),
                            existing_events=(),
                            event_type=OrderEventType.CREATED,
                            occurred_at=now,
                            available_at=now,
                            provenance=provenance,
                            source_cycle_id=cycle.cycle_id,
                        )
                    )
                    intent_ids.append(intent.order_intent_id)
                output = {
                    "status": (
                        "LLM_POLICY_EXPIRED_AWAITING_NEXT_REBALANCE"
                        if commit.overlay_state
                        is Q1OverlayState.EXPIRED_AWAITING_NEXT_REBALANCE
                        else "LLM_REDUCE_ONLY_COMMITTED"
                    ),
                    "portfolio_decision_id": (
                        commit.decision.portfolio_decision_id
                    ),
                    "policy_id": commit.policy_id,
                    "llm_overlay_state": commit.overlay_state.value,
                    "canceled_buy_order_event_ids": cancellation_ids,
                    "order_intent_ids": intent_ids,
                    "orders_created": len(intent_ids),
                    "real_order_routing": False,
                }
            complete_fenced_cycle(
                locked,
                cutoff=_aware(cycle.scheduled_at),
                input_manifest=cycle_input,
                output_manifest=output,
                completed_at=now,
            )
            return output

    def _complete_no_change(
        self,
        cycle: PaperCycleRow,
        *,
        now: datetime,
        scheduled_at: datetime,
        reason: str,
        context_manifest_hash: object | None = None,
        provider_audit: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        input_manifest = {
            "cycle_id": cycle.cycle_id,
            "scheduled_at": scheduled_at,
            "llm_context_manifest_hash": context_manifest_hash,
            "llm_provider_audit": provider_audit or {},
            "config_manifest_hash": self._runtime.config.manifest_hash,
            "real_order_routing": False,
        }
        output: dict[str, Any] = {
            "status": "LLM_NO_CHANGE",
            "reason": reason,
            "orders_created": 0,
            "real_order_routing": False,
        }
        if provider_audit:
            output["llm_provider_audit"] = provider_audit
        with self._session_factory.begin() as session:
            locked = require_cycle_fence(
                session,
                cycle_id=cycle.cycle_id,
                lease_owner=_lease_owner(cycle),
                attempt_count=cycle.attempt_count,
                fallback_now=now,
            )
            complete_fenced_cycle(
                locked,
                cutoff=scheduled_at,
                input_manifest=input_manifest,
                output_manifest=output,
                completed_at=now,
            )
        return output


def _normalized_target(
    values: Mapping[str, Decimal],
) -> dict[str, Decimal]:
    unknown = sorted(set(values) - {*RISKY_SYMBOLS, CASH_SYMBOL})
    if unknown:
        raise _ReviewUnavailable("Q1_DET_TARGET_UNIVERSE_VIOLATION")
    target = {
        symbol: Decimal(values.get(symbol, Decimal("0")))
        for symbol in RISKY_SYMBOLS
    }
    if any(value < 0 for value in target.values()):
        raise _ReviewUnavailable("Q1_DET_TARGET_INVALID")
    risky = sum(target.values(), Decimal("0"))
    if risky > Decimal("1"):
        raise _ReviewUnavailable("Q1_DET_TARGET_LEVERAGED")
    target[CASH_SYMBOL] = Decimal("1") - risky
    return target


def _used_normal_turnover(
    rows: tuple[tuple[FillRow, OrderIntentRow], ...],
    *,
    current_nav_usd: Decimal,
    as_of: datetime,
) -> tuple[Decimal, tuple[dict[str, object], ...]]:
    """Calculate filled non-emergency turnover from immutable typed records."""

    cutoff = require_aware_utc(as_of)
    if current_nav_usd <= 0:
        raise _ReviewUnavailable("TURNOVER_NAV_INVALID")
    value_deltas = {
        symbol: Decimal("0")
        for symbol in (*RISKY_SYMBOLS, CASH_SYMBOL)
    }
    references: list[dict[str, object]] = []
    allowed_classes = {
        Q1OrderClass.NORMAL,
        Q1OrderClass.LLM_REDUCTION,
    }
    known_classes = {
        *allowed_classes,
        Q1OrderClass.EMERGENCY_REDUCTION,
    }
    for fill_row, intent_row in rows:
        try:
            fill = Q1Fill.model_validate(fill_row.payload_json)
            intent = Q1OrderIntent.model_validate(intent_row.payload_json)
            order_class = Q1OrderClass(intent.order_class)
        except Exception as error:
            raise _ReviewUnavailable("TURNOVER_LEDGER_INVALID") from error
        if (
            fill.fill_id != fill_row.fill_id
            or fill.fill_hash != fill_row.fill_hash
            or fill.order_intent_id != intent.order_intent_id
            or intent.order_intent_id != intent_row.order_intent_id
            or intent.intent_hash != intent_row.intent_hash
            or fill.arm_id is not Q1ArmId.Q1_LLM
            or intent.arm_id is not Q1ArmId.Q1_LLM
            or fill.effective_at > cutoff
            or fill.symbol not in RISKY_SYMBOLS
            or order_class not in known_classes
        ):
            raise _ReviewUnavailable("TURNOVER_LEDGER_INVALID")
        if order_class not in allowed_classes:
            continue
        notional = fill.quantity * fill.price
        direction = (
            Decimal("1")
            if fill.side is OrderSide.BUY
            else Decimal("-1")
        )
        value_deltas[fill.symbol] += direction * notional
        value_deltas[CASH_SYMBOL] -= direction * notional
        references.append(
            {
                "fill_id": fill.fill_id,
                "fill_hash": fill.fill_hash,
                "order_intent_id": intent.order_intent_id,
                "intent_hash": intent.intent_hash,
                "order_class": order_class.value,
                "effective_at": fill.effective_at,
                "notional_usd": notional,
            }
        )
    one_way_turnover = (
        Decimal("0.5")
        * sum(
            (abs(delta) for delta in value_deltas.values()),
            Decimal("0"),
        )
        / current_nav_usd
    )
    return +one_way_turnover, tuple(references)


def _current_weights(
    state: Q1ArmState,
    prices: Mapping[str, Decimal],
) -> dict[str, Decimal]:
    nav = state.nav(dict(prices))
    weights = {
        symbol: state.positions.get(symbol, Decimal("0"))
        * prices[symbol]
        / nav
        for symbol in RISKY_SYMBOLS
    }
    risky = sum(weights.values(), Decimal("0"))
    if risky > Decimal("1"):
        raise _ReviewUnavailable("Q1_LLM_STATE_LEVERAGED")
    weights[CASH_SYMBOL] = Decimal("1") - risky
    return weights


def _assert_reduce_only(
    final: Mapping[str, Decimal],
    *,
    current: Mapping[str, Decimal],
    deterministic: Mapping[str, Decimal],
    enforce_deterministic_cap: bool,
) -> None:
    for symbol in RISKY_SYMBOLS:
        if (
            final[symbol] < 0
            or final[symbol] > current[symbol]
            or (
                enforce_deterministic_cap
                and final[symbol] > deterministic[symbol]
            )
        ):
            raise Q1LlmReviewError("Q1 LLM overlay attempted to increase risk")
    if sum(final.values(), Decimal("0")) != Decimal("1"):
        raise Q1LlmReviewError("Q1 LLM target weights must sum to one")


def _executable_target_quantities(
    *,
    state: Q1ArmState,
    final_target: Mapping[str, Decimal],
    nav_usd: Decimal,
    quotes: Mapping[str, DecisionQuote],
    order_book: Q1OrderBook,
    now: datetime,
    quantity_increment: Decimal,
    minimum_notional: Decimal,
) -> dict[str, Decimal]:
    pending_sell_quantities = {
        symbol: Decimal("0")
        for symbol in RISKY_SYMBOLS
    }
    for aggregate in pending_orders(
        order_book.descriptors,
        order_book.events,
        as_of=now,
    ):
        if (
            aggregate.order.side is OrderSide.SELL
            and aggregate.order.symbol in pending_sell_quantities
            and aggregate.order.valid_until > now
        ):
            pending_sell_quantities[
                aggregate.order.symbol
            ] += aggregate.remaining_quantity

    executable: dict[str, Decimal] = {}
    for symbol in RISKY_SYMBOLS:
        current = state.positions.get(symbol, Decimal("0"))
        desired_target = min(
            current,
            (
                final_target[symbol]
                * nav_usd
                / quotes[symbol].midpoint
            ),
        )
        desired_reduction = max(
            Decimal("0"),
            current - desired_target,
        )
        residual_reduction = max(
            Decimal("0"),
            desired_reduction - pending_sell_quantities[symbol],
        ).quantize(quantity_increment, rounding=ROUND_DOWN)
        if (
            residual_reduction > 0
            and residual_reduction * quotes[symbol].bid
            < minimum_notional
        ):
            residual_reduction = Decimal("0")
        executable[symbol] = current - residual_reduction
    return executable


def _buy_cancellations(
    order_book: Q1OrderBook,
    *,
    occurred_at: datetime,
    provenance: OrderEventProvenance,
    source_cycle_id: str,
) -> tuple[OrderEvent, ...]:
    events: list[OrderEvent] = []
    for aggregate in pending_orders(
        order_book.descriptors,
        order_book.events,
        as_of=occurred_at,
    ):
        if aggregate.order.side is not OrderSide.BUY:
            continue
        events.append(
            append_order_event(
                order=aggregate.order,
                existing_events=(*order_book.events, *events),
                event_type=OrderEventType.CANCELED_BY_RISK,
                occurred_at=occurred_at,
                available_at=occurred_at,
                provenance=provenance,
                reason="Q1_LLM_REDUCE_ONLY_BLOCK_NEW_BUYS",
                source_cycle_id=source_cycle_id,
            )
        )
    return tuple(events)


def _expected_volatility(
    deterministic: Q1StrategyDecision,
    weights: Mapping[str, Decimal],
) -> Decimal:
    signal = deterministic.diagnostics.get("signal")
    if not isinstance(signal, dict):
        return Decimal("0")
    covariance = cast(dict[str, object], signal).get("covariance")
    if not isinstance(covariance, dict):
        return Decimal("0")
    typed_covariance = cast(dict[str, object], covariance)
    variance = Decimal("0")
    try:
        for left in RISKY_SYMBOLS:
            row = typed_covariance[left]
            if not isinstance(row, dict):
                return Decimal("0")
            typed_row = cast(dict[str, object], row)
            for right in RISKY_SYMBOLS:
                variance += (
                    weights[left]
                    * weights[right]
                    * Decimal(str(typed_row[right]))
                )
    except (KeyError, ValueError):
        return Decimal("0")
    return Decimal("0") if variance <= 0 else variance.sqrt()


def _one_way_turnover(
    current: Mapping[str, Decimal],
    target: Mapping[str, Decimal],
) -> Decimal:
    return Decimal("0.5") * sum(
        (
            abs(target[symbol] - current[symbol])
            for symbol in (*RISKY_SYMBOLS, CASH_SYMBOL)
        ),
        Decimal("0"),
    )


def _diagnostic_string(
    decision: Q1StrategyDecision,
    key: str,
) -> str | None:
    value = decision.diagnostics.get(key)
    return None if value is None else str(value)


def _expired_policy_id(
    decision: Q1StrategyDecision | None,
    *,
    now: datetime,
) -> str | None:
    if decision is None:
        return None
    if (
        decision.diagnostics.get("llm_overlay_state")
        != Q1OverlayState.ACTIVE.value
    ):
        return None
    expiry = _previous_policy_expiry(decision)
    if expiry is None or expiry > now:
        return None
    value = decision.diagnostics.get("llm_policy_id")
    return None if value is None else str(value)


def _previous_policy_expiry(
    decision: Q1StrategyDecision | None,
) -> datetime | None:
    if decision is None:
        return None
    raw = decision.diagnostics.get("llm_policy_expiry_time")
    if not isinstance(raw, str):
        return None
    try:
        return _aware(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except ValueError:
        return None


def _order_book_hash(order_book: Q1OrderBook) -> str:
    return canonical_hash(
        {
            "intents": order_book.intents,
            "events": order_book.events,
        }
    )


def _descriptor(intent: Q1OrderIntent) -> OrderDescriptor:
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


def _inside_regular_session(
    instant: datetime,
    calendar: VersionedMarketSession,
) -> bool:
    return calendar.open_at <= instant < calendar.close_at


def _session_clock(
    calendar: VersionedMarketSession,
    value: time,
    *,
    timezone: Any,
) -> datetime:
    return datetime.combine(
        calendar.session_date,
        value,
        tzinfo=timezone,
    ).astimezone(UTC)


def _mapping(document: dict[str, Any], key: str) -> dict[str, Any]:
    value = document.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"Q1 config {key!r} must be an object")
    return cast(dict[str, Any], value)


def _positive_int(document: dict[str, Any], key: str) -> int:
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"Q1 config {key!r} must be a positive integer")
    return value


def _positive_decimal(
    document: dict[str, Any],
    key: str,
) -> Decimal:
    value = Decimal(str(document[key]))
    if not value.is_finite() or value <= 0:
        raise ValueError(f"Q1 config {key!r} must be positive")
    return value


def _parse_clock(raw: str) -> time:
    try:
        return datetime.strptime(raw, "%H:%M").time()
    except ValueError as exc:
        raise ValueError(f"Invalid ET clock {raw!r}; expected HH:MM") from exc


def _lease_owner(cycle: PaperCycleRow) -> str:
    if not cycle.lease_owner:
        raise Q1LlmReviewError("Q1 LLM review cycle has no lease owner")
    return cycle.lease_owner


def _provider_audit_payload(
    provider: object | None,
    request_id: str,
) -> dict[str, object] | None:
    if provider is None:
        return None
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
    return (
        value.replace(tzinfo=UTC)
        if value.tzinfo is None
        else value.astimezone(UTC)
    )
