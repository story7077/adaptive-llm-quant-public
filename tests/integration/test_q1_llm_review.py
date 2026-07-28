from __future__ import annotations

import time
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trading.data.alpaca import FEED, PROVIDER
from trading.domain.contracts import NewsEvent, model_payload
from trading.domain.enums import MarketConnectionState, OrderSide, OrdinalBucket
from trading.domain.hashing import canonical_hash, stable_id
from trading.domain.q1 import (
    MarketCalendarSession,
    OrderEventType,
    Q1ArmId,
    Q1DecisionInputManifest,
    Q1StrategyDecision,
)
from trading.domain.q1_runtime import Q1OrderIntent
from trading.domain.time import FrozenClock
from trading.execution.order_state import (
    OrderDescriptor,
    OrderEventProvenance,
    Q1OrderClass,
    append_order_event,
)
from trading.llm.q1_overlay import Q1LlmOverlayDecision
from trading.persistence.models import (
    MarketQuoteRow,
    MarketStreamStatusRow,
    NewsEventRow,
    OrderEventRow,
    OrderIntentRow,
    PaperCycleRow,
    PortfolioDecisionRow,
    RunRow,
)
from trading.persistence.q1 import (
    MarketCalendarSessionRepository,
    OrderEventRepository,
)
from trading.persistence.q1_runtime import (
    append_arm_state,
    append_order_intent,
    append_strategy_decision,
    complete_fenced_cycle,
)
from trading.runtime.q1_llm_review import Q1LlmReviewCycleProcessor
from trading.runtime.q1_paper import Q1PaperRuntimeService
from trading.runtime.q1_planning import build_portfolio_decision
from trading.runtime.q1_state import Q1ArmState
from trading.settings import (
    Q1ConfigBundle,
    load_q1_config_bundle,
)

NOW = datetime(2026, 7, 27, 16, 0, tzinfo=UTC)
STRATEGIC_AT = datetime(2026, 7, 27, 14, 0, tzinfo=UTC)
SESSION_OPEN = datetime(2026, 7, 27, 13, 30, tzinfo=UTC)
SESSION_CLOSE = datetime(2026, 7, 27, 20, 0, tzinfo=UTC)
CODE_VERSION = "q1-llm-review-test"
MODEL_VERSION = "q1-test-model"


def test_noon_llm_review_commits_sell_only_and_cancels_pending_buy(
    sqlite_database: tuple[
        str,
        Any,
        sessionmaker[Session],
    ],
    repository_root: Path,
) -> None:
    _, _, factory = sqlite_database
    config = load_q1_config_bundle(repository_root / "config")
    cycle = _seed_runtime(factory, config=config)
    seen_request: dict[str, Any] = {}

    def provider(request: dict[str, Any]) -> Q1LlmOverlayDecision:
        seen_request.update(request)
        return Q1LlmOverlayDecision(
            request_id=str(request["request_id"]),
            context_manifest_hash=str(request["context_manifest_hash"]),
            risk_multiplier=0.5,
            block_new_entries=True,
            evidence_event_ids=["news-1"],
            rationale="Macro evidence supports a temporary reduction.",
            effective_time=NOW,
            expiry_time=NOW + timedelta(hours=1),
            created_at=NOW,
        )

    processor = _processor(
        factory,
        config=config,
        repository_root=repository_root,
        provider=provider,
    )
    result = processor.process(cycle)

    assert result["status"] == "LLM_REDUCE_ONLY_COMMITTED"
    assert result["orders_created"] == 2
    assert result["real_order_routing"] is False
    assert seen_request["allowed_evidence_event_ids"] == ["news-1"]
    assert seen_request["real_order_routing"] is False

    with factory() as session:
        review_decision = session.scalar(
            select(PortfolioDecisionRow).where(
                PortfolioDecisionRow.source_cycle_id == cycle.cycle_id
            )
        )
        assert review_decision is not None
        typed = Q1StrategyDecision.model_validate(
            review_decision.payload_json
        )
        assert typed.target_weights == {
            "QQQ": Decimal("0.10"),
            "SOXX": Decimal("0.10"),
            "USD_CASH": Decimal("0.80"),
        }
        assert Decimal(
            str(typed.diagnostics["expected_one_way_turnover"])
        ) == Decimal("0.20")
        assert Decimal(
            str(typed.diagnostics["used_daily_turnover_before"])
        ) == Decimal("0")
        normal_turnover = typed.diagnostics["normal_turnover"]
        assert isinstance(normal_turnover, dict)
        assert Decimal(
            str(normal_turnover["interpolation_alpha"])
        ) == Decimal("0.6666666666666666666666666667")
        intents = tuple(
            session.scalars(
                select(OrderIntentRow)
                .where(OrderIntentRow.source_cycle_id == cycle.cycle_id)
                .order_by(OrderIntentRow.symbol)
            )
        )
        assert [row.side for row in intents] == ["SELL", "SELL"]
        assert [
            row.payload_json["order_class"]
            for row in intents
        ] == ["LLM_REDUCTION", "LLM_REDUCTION"]
        assert [row.quantity for row in intents] == [
            Decimal("1.0000000000"),
            Decimal("0.7500000000"),
        ]
        buy_events = tuple(
            session.scalars(
                select(OrderEventRow)
                .where(OrderEventRow.order_intent_id == "pending-buy")
                .order_by(OrderEventRow.event_sequence)
            )
        )
        assert [row.event_type for row in buy_events] == [
            "CREATED",
            "CANCELED_BY_RISK",
        ]
        sell_events = tuple(
            session.scalars(
                select(OrderEventRow)
                .where(OrderEventRow.order_intent_id == "pending-sell")
                .order_by(OrderEventRow.event_sequence)
            )
        )
        assert [row.event_type for row in sell_events] == ["CREATED"]


def test_outside_evidence_completes_fenced_no_change(
    sqlite_database: tuple[
        str,
        Any,
        sessionmaker[Session],
    ],
    repository_root: Path,
) -> None:
    _, _, factory = sqlite_database
    config = load_q1_config_bundle(repository_root / "config")
    cycle = _seed_runtime(factory, config=config)

    def provider(request: dict[str, Any]) -> Q1LlmOverlayDecision:
        return Q1LlmOverlayDecision(
            request_id=str(request["request_id"]),
            context_manifest_hash=str(request["context_manifest_hash"]),
            risk_multiplier=0.5,
            block_new_entries=True,
            evidence_event_ids=["not-in-request"],
            rationale="Invalid evidence reference.",
            effective_time=NOW,
            expiry_time=NOW + timedelta(hours=1),
            created_at=NOW,
        )

    result = _processor(
        factory,
        config=config,
        repository_root=repository_root,
        provider=provider,
    ).process(cycle)

    assert result == {
        "status": "LLM_NO_CHANGE",
        "reason": "INVALID_PROVIDER_OUTPUT",
        "orders_created": 0,
        "real_order_routing": False,
    }
    with factory() as session:
        stored_cycle = session.get(PaperCycleRow, cycle.cycle_id)
        assert stored_cycle is not None
        assert stored_cycle.status == "COMPLETED"
        assert session.scalar(
            select(PortfolioDecisionRow).where(
                PortfolioDecisionRow.source_cycle_id == cycle.cycle_id
            )
        ) is None
        events = tuple(
            session.scalars(
                select(OrderEventRow).where(
                    OrderEventRow.order_intent_id == "pending-buy"
                )
            )
        )
        assert [event.event_type for event in events] == ["CREATED"]


def test_provider_timeout_does_not_fail_deterministic_lane(
    sqlite_database: tuple[
        str,
        Any,
        sessionmaker[Session],
    ],
    repository_root: Path,
) -> None:
    _, _, factory = sqlite_database
    loaded = load_q1_config_bundle(repository_root / "config")
    document = deepcopy(loaded.document)
    document["llm"]["provider_timeout_seconds"] = 0.01
    config = Q1ConfigBundle(
        document=document,
        cost_document=loaded.cost_document,
        manifest_hash=canonical_hash(
            {
                "q1-math-core.yaml": document,
                "costs.yaml": loaded.cost_document,
            }
        ),
    )
    cycle = _seed_runtime(factory, config=config)

    def provider(_request: dict[str, Any]) -> None:
        time.sleep(0.1)

    result = _processor(
        factory,
        config=config,
        repository_root=repository_root,
        provider=provider,
    ).process(cycle)

    assert result["status"] == "LLM_NO_CHANGE"
    assert result["reason"] == "PROVIDER_TIMEOUT"
    with factory() as session:
        stored_cycle = session.get(PaperCycleRow, cycle.cycle_id)
        assert stored_cycle is not None
        assert stored_cycle.status == "COMPLETED"


def test_expired_policy_records_wait_state_without_buy(
    sqlite_database: tuple[
        str,
        Any,
        sessionmaker[Session],
    ],
    repository_root: Path,
) -> None:
    _, _, factory = sqlite_database
    config = load_q1_config_bundle(repository_root / "config")
    cycle = _seed_runtime(
        factory,
        config=config,
        previous_policy_expiry=NOW - timedelta(minutes=1),
    )
    provider_called = False

    def provider(_request: dict[str, Any]) -> None:
        nonlocal provider_called
        provider_called = True

    result = _processor(
        factory,
        config=config,
        repository_root=repository_root,
        provider=provider,
    ).process(cycle)

    assert provider_called is False
    assert (
        result["status"]
        == "LLM_POLICY_EXPIRED_AWAITING_NEXT_REBALANCE"
    )
    assert result["orders_created"] == 0
    with factory() as session:
        decision_row = session.scalar(
            select(PortfolioDecisionRow).where(
                PortfolioDecisionRow.source_cycle_id == cycle.cycle_id
            )
        )
        assert decision_row is not None
        decision = Q1StrategyDecision.model_validate(
            decision_row.payload_json
        )
        assert (
            decision.diagnostics["llm_overlay_state"]
            == "EXPIRED_AWAITING_NEXT_REBALANCE"
        )
        assert not tuple(
            session.scalars(
                select(OrderIntentRow).where(
                    OrderIntentRow.source_cycle_id == cycle.cycle_id,
                    OrderIntentRow.side == "BUY",
                )
            )
        )
        buy_latest = session.scalar(
            select(OrderEventRow)
            .where(OrderEventRow.order_intent_id == "pending-buy")
            .order_by(OrderEventRow.event_sequence.desc())
            .limit(1)
        )
        assert buy_latest is not None
        assert buy_latest.event_type == "CANCELED_BY_RISK"


def test_review_delayed_to_policy_cutoff_does_not_call_provider(
    sqlite_database: tuple[
        str,
        Any,
        sessionmaker[Session],
    ],
    repository_root: Path,
) -> None:
    _, _, factory = sqlite_database
    config = load_q1_config_bundle(repository_root / "config")
    cycle = _seed_runtime(factory, config=config)
    delayed_now = datetime(2026, 7, 27, 17, 0, tzinfo=UTC)
    with factory.begin() as session:
        _seed_quotes(session, quote_now=delayed_now, suffix="-cutoff")
        status = session.get(MarketStreamStatusRow, (PROVIDER, FEED))
        assert status is not None
        status.last_message_at = delayed_now - timedelta(seconds=1)
        status.last_quote_at = delayed_now - timedelta(seconds=1)
        status.updated_at = delayed_now
    provider_called = False

    def provider(_request: dict[str, Any]) -> None:
        nonlocal provider_called
        provider_called = True

    result = _processor(
        factory,
        config=config,
        repository_root=repository_root,
        provider=provider,
        now=delayed_now,
    ).process(cycle)

    assert provider_called is False
    assert result["status"] == "LLM_NO_CHANGE"
    assert result["reason"] == "NEW_POLICY_CUTOFF_REACHED"


def _processor(
    factory: sessionmaker[Session],
    *,
    config: Q1ConfigBundle,
    repository_root: Path,
    provider: Any,
    now: datetime = NOW,
) -> Q1LlmReviewCycleProcessor:
    runtime = Q1PaperRuntimeService(
        factory,
        config=config,
        workspace_root=repository_root,
        clock=FrozenClock(now),
    )
    return Q1LlmReviewCycleProcessor(
        factory,
        runtime=runtime,
        workspace_root=repository_root,
        llm_overlay_provider=provider,
        clock=FrozenClock(now),
        code_version=CODE_VERSION,
        model_version=MODEL_VERSION,
    )


def _seed_runtime(
    factory: sessionmaker[Session],
    *,
    config: Q1ConfigBundle,
    previous_policy_expiry: datetime | None = None,
) -> PaperCycleRow:
    source_hash = canonical_hash("q1-llm-review-seed")
    calendar = MarketCalendarSession(
        calendar_session_id="calendar-2026-07-27",
        calendar_version=str(
            config.document["market_calendar_version"]
        ),
        session_date=date(2026, 7, 27),
        open_at=SESSION_OPEN,
        close_at=SESSION_CLOSE,
        source="TEST",
        available_at=SESSION_OPEN - timedelta(days=1),
        session_hash=canonical_hash("calendar"),
        created_at=SESSION_OPEN - timedelta(days=1),
        config_manifest_hash=config.manifest_hash,
        code_version=CODE_VERSION,
        model_version=MODEL_VERSION,
        source_manifest_hash=source_hash,
    )
    with factory.begin() as session:
        session.add(
            RunRow(
                run_id="q1-llm-review-run",
                mode="PAPER",
                experiment_version="q1_math_core_v1",
                config_manifest_hash=config.manifest_hash,
                code_commit=CODE_VERSION,
                started_at=SESSION_OPEN,
                ended_at=None,
                status="RUNNING",
                result_manifest={},
                result_hash=None,
            )
        )
        MarketCalendarSessionRepository(session).append(calendar)
        seed_cycle = _cycle(
            cycle_id="seed-strategic-cycle",
            cycle_kind="Q1_STRATEGIC",
            scheduled_at=STRATEGIC_AT,
            lease_owner="seed-worker",
        )
        session.add(seed_cycle)
        session.flush()
        state = Q1ArmState(
            arm_id=Q1ArmId.Q1_LLM.value,
            initial_nav_usd=Decimal("1000"),
            settled_cash_usd=Decimal("600"),
            unsettled_receivables=(),
            positions={
                "QQQ": Decimal("2"),
                "SOXX": Decimal("2"),
            },
            sequence=0,
            evaluation_anchor_id="anchor-1",
        )
        append_arm_state(
            session,
            run_id="q1-llm-review-run",
            state=state,
            source_cycle_id=seed_cycle.cycle_id,
            created_at=STRATEGIC_AT,
            expected_previous_sequence=None,
        )
        manifest_content = {
            "calendar_session_id": calendar.calendar_session_id,
            "source_bars": (),
            "quotes": (),
            "config_manifest_hash": config.manifest_hash,
            "code_version": CODE_VERSION,
            "model_version": MODEL_VERSION,
            "source_manifest_hash": source_hash,
        }
        manifest = Q1DecisionInputManifest(
            calendar_session_id=calendar.calendar_session_id,
            source_bars=(),
            quotes=(),
            config_manifest_hash=config.manifest_hash,
            code_version=CODE_VERSION,
            model_version=MODEL_VERSION,
            source_manifest_hash=source_hash,
            manifest_hash=canonical_hash(manifest_content),
        )
        deterministic = _seed_decision(
            arm_id=Q1ArmId.Q1_DET,
            cycle=seed_cycle,
            manifest=manifest,
            target={
                "QQQ": Decimal("0.10"),
                "SOXX": Decimal("0.10"),
                "USD_CASH": Decimal("0.80"),
            },
            overlay_state="NOT_APPLICABLE",
            policy_id=None,
            expiry=None,
        )
        previous_llm = _seed_decision(
            arm_id=Q1ArmId.Q1_LLM,
            cycle=seed_cycle,
            manifest=manifest,
            target={
                "QQQ": Decimal("0.20"),
                "SOXX": Decimal("0.20"),
                "USD_CASH": Decimal("0.60"),
            },
            overlay_state=(
                "ACTIVE"
                if previous_policy_expiry is not None
                else "NO_CHANGE"
            ),
            policy_id=(
                "previous-policy"
                if previous_policy_expiry is not None
                else None
            ),
            expiry=previous_policy_expiry,
        )
        append_strategy_decision(session, decision=deterministic)
        append_strategy_decision(session, decision=previous_llm)
        event_repository = OrderEventRepository(session)
        provenance = OrderEventProvenance(
            config_manifest_hash=config.manifest_hash,
            code_version=CODE_VERSION,
            model_version=MODEL_VERSION,
            source_manifest_hash=source_hash,
            worker_fence_token="seed-worker",
            cycle_attempt_count=1,
        )
        for intent in (
            _pending_intent(
                order_id="pending-buy",
                side=OrderSide.BUY,
                symbol="QQQ",
                decision=previous_llm,
            ),
            _pending_intent(
                order_id="pending-sell",
                side=OrderSide.SELL,
                symbol="SOXX",
                decision=previous_llm,
            ),
        ):
            append_order_intent(session, intent)
            session.flush()
            descriptor = OrderDescriptor(
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
            event_repository.append(
                append_order_event(
                    order=descriptor,
                    existing_events=(),
                    event_type=OrderEventType.CREATED,
                    occurred_at=STRATEGIC_AT,
                    available_at=STRATEGIC_AT,
                    provenance=provenance,
                    source_cycle_id=seed_cycle.cycle_id,
                )
            )
        complete_fenced_cycle(
            seed_cycle,
            cutoff=STRATEGIC_AT,
            input_manifest={"seed": True},
            output_manifest={"seed": True},
            completed_at=STRATEGIC_AT,
        )
        review_cycle = _cycle(
            cycle_id="noon-llm-review-cycle",
            cycle_kind="Q1_LLM_REVIEW",
            scheduled_at=NOW,
            lease_owner="review-worker",
        )
        session.add(review_cycle)
        _seed_news(session)
        _seed_quotes(session)
        session.add(
            MarketStreamStatusRow(
                provider=PROVIDER,
                feed=FEED,
                state=MarketConnectionState.CONNECTED.value,
                connected_at=SESSION_OPEN,
                disconnected_at=None,
                last_message_at=NOW - timedelta(seconds=1),
                last_bar_at=None,
                last_quote_at=NOW - timedelta(seconds=1),
                last_trade_at=None,
                reconnect_count=0,
                consecutive_failures=0,
                last_error_code=None,
                last_error_detail=None,
                updated_at=NOW,
            )
        )
    return review_cycle


def _seed_decision(
    *,
    arm_id: Q1ArmId,
    cycle: PaperCycleRow,
    manifest: Q1DecisionInputManifest,
    target: dict[str, Decimal],
    overlay_state: str,
    policy_id: str | None,
    expiry: datetime | None,
) -> Q1StrategyDecision:
    return build_portfolio_decision(
        run_id="q1-llm-review-run",
        arm_id=arm_id,
        source_cycle_id=cycle.cycle_id,
        input_state_sequence=0,
        decision_kind="STRATEGIC",
        scheduled_at=STRATEGIC_AT,
        signal_data_cutoff=STRATEGIC_AT,
        portfolio_state_as_of=STRATEGIC_AT,
        quote_as_of=STRATEGIC_AT,
        decision_created_at=STRATEGIC_AT,
        valid_until=STRATEGIC_AT + timedelta(minutes=20),
        current_weights=target,
        deterministic_target_weights=target,
        final_target_weights=target,
        expected_annualized_volatility=Decimal("0.10"),
        expected_one_way_turnover=Decimal("0"),
        used_daily_turnover_before=Decimal("0"),
        signal_hash="signal-1",
        allocation_hash="allocation-1",
        llm_overlay_state=overlay_state,
        llm_policy_id=policy_id,
        diagnostics={
            "signal": {
                "covariance": {
                    "QQQ": {
                        "QQQ": "0.04",
                        "SOXX": "0.01",
                    },
                    "SOXX": {
                        "QQQ": "0.01",
                        "SOXX": "0.09",
                    },
                }
            },
            "llm_policy_expiry_time": expiry,
        },
        input_manifest=manifest,
        worker_fence_token=str(cycle.lease_owner),
        cycle_attempt_count=cycle.attempt_count,
    )


def _pending_intent(
    *,
    order_id: str,
    side: OrderSide,
    symbol: str,
    decision: Q1StrategyDecision,
) -> Q1OrderIntent:
    identity = {
        "order_id": order_id,
        "side": side,
        "symbol": symbol,
    }
    intent_hash = canonical_hash(identity)
    return Q1OrderIntent(
        order_intent_id=order_id,
        run_id=decision.run_id,
        arm_id=Q1ArmId.Q1_LLM,
        portfolio_decision_id=decision.portfolio_decision_id,
        risk_decision_id=stable_id("risk", identity),
        source_cycle_id=decision.source_cycle_id,
        input_state_sequence=0,
        symbol=symbol,
        side=side,
        order_class=Q1OrderClass.NORMAL.value,
        quantity=Decimal("0.25"),
        decision_quote_id=f"seed-{symbol}",
        decision_reference_price=Decimal("100"),
        decision_spread_bps=Decimal("2"),
        created_at=STRATEGIC_AT,
        valid_until=SESSION_CLOSE,
        idempotency_key=stable_id("intent-idem", identity),
        algorithm_version="q1_math_core_v1",
        config_manifest_hash=decision.config_manifest_hash,
        code_version=CODE_VERSION,
        model_version=MODEL_VERSION,
        source_manifest_hash=decision.source_manifest_hash,
        intent_hash=intent_hash,
    )


def _seed_news(session: Session) -> None:
    event = NewsEvent(
        news_event_id="news-1",
        schema_version="news_event_v2",
        model_run_id="news-model-run",
        as_of=NOW - timedelta(minutes=5),
        data_available_cutoff=NOW - timedelta(minutes=5),
        source_event_ids=["source-1"],
        event_type="MACRO",
        actors=[],
        facts=[],
        impacts=[],
        novelty_bucket=OrdinalBucket.MEDIUM,
        contradiction_source_ids=[],
        invalidation_conditions=[],
        expires_at=NOW + timedelta(hours=2),
        prompt_hash=canonical_hash("prompt"),
        context_manifest_hash=canonical_hash("news-context"),
        output_hash=canonical_hash("news-output"),
        created_at=NOW - timedelta(minutes=4),
    )
    session.add(
        NewsEventRow(
            news_event_id=event.news_event_id,
            as_of=event.as_of,
            payload_json=model_payload(event),
            output_hash=event.output_hash,
        )
    )


def _seed_quotes(
    session: Session,
    *,
    quote_now: datetime = NOW,
    suffix: str = "",
) -> None:
    for symbol in ("QQQ", "SOXX"):
        instant = quote_now - timedelta(seconds=1)
        payload_hash = canonical_hash(
            {
                "symbol": symbol,
                "event_time": instant,
            }
        )
        session.add(
            MarketQuoteRow(
                quote_id=f"quote-{symbol}{suffix}",
                provider=PROVIDER,
                feed=FEED,
                symbol=symbol,
                event_time=instant,
                provider_timestamp=instant.isoformat(),
                available_at=instant,
                ingested_at=instant,
                source_kind="STREAM_QUOTE",
                bid_exchange="V",
                bid_price=Decimal("99.99"),
                bid_size_round_lots=10,
                ask_exchange="V",
                ask_price=Decimal("100.01"),
                ask_size_round_lots=10,
                conditions=[],
                tape="C",
                payload_hash=payload_hash,
                raw_object_uri=None,
                payload_json={},
            )
        )


def _cycle(
    *,
    cycle_id: str,
    cycle_kind: str,
    scheduled_at: datetime,
    lease_owner: str,
) -> PaperCycleRow:
    return PaperCycleRow(
        cycle_id=cycle_id,
        run_id="q1-llm-review-run",
        cycle_kind=cycle_kind,
        scheduled_at=scheduled_at,
        data_available_cutoff=None,
        status="RUNNING",
        idempotency_key=cycle_id,
        lease_owner=lease_owner,
        lease_expires_at=SESSION_CLOSE + timedelta(hours=1),
        attempt_count=1,
        input_manifest_hash=None,
        output_manifest_hash=None,
        started_at=scheduled_at,
        completed_at=None,
        last_error_code=None,
        last_error_detail=None,
        created_at=scheduled_at,
        updated_at=scheduled_at,
    )
