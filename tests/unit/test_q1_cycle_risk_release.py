# pyright: reportPrivateUsage=false

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from trading.domain.hashing import canonical_hash
from trading.domain.q1 import (
    Q1ArmId,
    RiskEpisode,
    RiskEpisodeEvent,
    RiskEpisodeEventType,
    RiskSeverity,
    RiskTarget,
)
from trading.persistence.models import NavSnapshotRow, PaperCycleRow, RunRow
from trading.runtime.q1_cycle import (
    Q1CycleError,
    Q1CycleNotReady,
    Q1PaperCycleProcessor,
    _general_weights,
    _one_way_turnover,
    _target_quantity_weights,
)
from trading.runtime.q1_planning import DecisionQuote
from trading.runtime.q1_scheduler import VersionedMarketSession
from trading.runtime.q1_state import Q1ArmState
from trading.settings import load_q1_config_bundle

NOW = datetime(2026, 7, 28, 14, 0, tzinfo=UTC)
HASH = "a" * 64


def _cycle() -> PaperCycleRow:
    return PaperCycleRow(
        cycle_id="strategic-release-cycle",
        run_id="q1-release-run",
        cycle_kind="Q1_STRATEGIC",
        scheduled_at=NOW,
        data_available_cutoff=NOW,
        status="RUNNING",
        idempotency_key="strategic-release-cycle",
        lease_owner="worker-release",
        lease_expires_at=NOW + timedelta(hours=1),
        attempt_count=1,
        input_manifest_hash=None,
        output_manifest_hash=None,
        started_at=NOW,
        completed_at=None,
        last_error_code=None,
        last_error_detail=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _calendar() -> VersionedMarketSession:
    return VersionedMarketSession(
        calendar_session_id="calendar-current",
        calendar_version="alpaca_market_calendar_v1",
        session_date=date(2026, 7, 28),
        open_at=NOW - timedelta(minutes=30),
        close_at=NOW + timedelta(hours=6),
        source_payload_hash=HASH,
        source_available_at=NOW - timedelta(days=1),
    )


def _state() -> Q1ArmState:
    return Q1ArmState(
        arm_id=Q1ArmId.Q1_DET.value,
        initial_nav_usd=Decimal("1000"),
        settled_cash_usd=Decimal("500"),
        unsettled_receivables=(),
        positions={"QQQ": Decimal("1")},
        sequence=4,
        evaluation_anchor_id="anchor",
    )


def _episode() -> tuple[RiskEpisode, tuple[RiskEpisodeEvent, ...]]:
    target = RiskTarget(
        symbol="QQQ",
        target_quantity=Decimal("0.5"),
        trigger_quote_id="quote-trigger",
        target_generation=1,
        target_id="target-qqq",
        trigger_quantity=Decimal("1"),
        trigger_price=Decimal("500"),
        target_weight=Decimal("0.25"),
    )
    episode = RiskEpisode(
        risk_episode_id="episode-hard",
        run_id="q1-release-run",
        arm_id=Q1ArmId.Q1_DET,
        severity=RiskSeverity.HARD_REDUCE,
        calendar_session_id="calendar-previous",
        triggered_at=NOW - timedelta(days=1),
        trigger_nav_usd=Decimal("1000"),
        session_open_nav_usd=Decimal("1050"),
        running_peak_nav_usd=Decimal("1100"),
        daily_loss=Decimal("0.05"),
        run_drawdown=Decimal("0.09"),
        portfolio_annualized_vol=None,
        soft_daily_threshold=Decimal("0.015"),
        hard_daily_threshold=Decimal("0.025"),
        reconciliation_status="OK",
        targets=(target,),
        target_manifest_hash=canonical_hash((target,)),
        episode_hash=canonical_hash({"episode": "hard"}),
        created_at=NOW - timedelta(days=1),
        config_manifest_hash=HASH,
        code_version="test-code",
        model_version="test-model",
        source_manifest_hash=HASH,
    )
    activation = RiskEpisodeEvent(
        risk_episode_event_id="episode-hard-activate",
        risk_episode_id=episode.risk_episode_id,
        event_type=RiskEpisodeEventType.ACTIVATE,
        event_sequence=1,
        severity=RiskSeverity.HARD_REDUCE,
        target_generation=1,
        occurred_at=NOW - timedelta(days=1),
        available_at=NOW - timedelta(days=1),
        targets=(target,),
        source_cycle_id="previous-risk-cycle",
        worker_fence_token="previous-worker",
        cycle_attempt_count=1,
        idempotency_key="episode-hard-activate",
        event_hash=canonical_hash({"event": "activate"}),
        config_manifest_hash=HASH,
        code_version="test-code",
        model_version="test-model",
        source_manifest_hash=HASH,
    )
    return episode, (activation,)


def test_achieved_latched_target_does_not_require_a_fresh_quote() -> None:
    state = Q1ArmState(
        arm_id=Q1ArmId.Q1_DET.value,
        initial_nav_usd=Decimal("1000"),
        settled_cash_usd=Decimal("100"),
        unsettled_receivables=(),
        positions={
            "QQQ": Decimal("1"),
            "SOXX": Decimal("5"),
        },
        sequence=3,
        evaluation_anchor_id="anchor",
    )
    targets = {
        "QQQ": RiskTarget(
            symbol="QQQ",
            target_quantity=Decimal("2"),
            trigger_quote_id="trigger-qqq",
            trigger_price=Decimal("100"),
        ),
        "SOXX": RiskTarget(
            symbol="SOXX",
            target_quantity=Decimal("2"),
            trigger_quote_id="trigger-soxx",
            trigger_price=Decimal("50"),
        ),
    }
    fresh = {
        "SOXX": DecisionQuote(
            symbol="SOXX",
            quote_id="fresh-soxx",
            bid=Decimal("49.99"),
            ask=Decimal("50.01"),
            available_at=NOW,
        )
    }

    quotes = Q1PaperCycleProcessor._risk_check_quotes(
        arm_id=Q1ArmId.Q1_DET.value,
        state=state,
        fresh_quotes=fresh,
        active_targets=targets,
    )

    assert quotes["QQQ"].quote_id == "trigger-qqq"
    assert quotes["SOXX"].quote_id == "fresh-soxx"
    with pytest.raises(Q1CycleNotReady, match="SOXX"):
        Q1PaperCycleProcessor._risk_check_quotes(
            arm_id=Q1ArmId.Q1_DET.value,
            state=state,
            fresh_quotes={},
            active_targets=targets,
        )


def _processor(
    repository_root: Path,
    *,
    valid_checks: int,
    include_episode: bool = True,
) -> Q1PaperCycleProcessor:
    processor = object.__new__(Q1PaperCycleProcessor)
    untyped = cast(Any, processor)
    untyped._runtime = type(
        "RuntimeStub",
        (),
        {"config": load_q1_config_bundle(repository_root / "config")},
    )()
    untyped._workspace_root = repository_root
    episode, events = _episode()

    def active_episode(
        *,
        run_id: str,
        arm_id: str,
    ) -> tuple[RiskEpisode | None, tuple[RiskEpisodeEvent, ...]]:
        del run_id
        return (
            (episode, events)
            if (
                include_episode
                and arm_id == Q1ArmId.Q1_DET.value
            )
            else (None, ())
        )

    def nav_baselines(**_kwargs: object) -> tuple[Decimal, Decimal]:
        return Decimal("1000"), Decimal("1000")

    def current_volatility(
        run_id: str,
        arm_id: str,
        **_kwargs: object,
    ) -> Decimal | None:
        del run_id, arm_id
        return None

    def release_checks(**_kwargs: object) -> int:
        return valid_checks

    untyped._active_risk_episode = active_episode
    untyped._risk_nav_baselines = nav_baselines
    untyped._current_portfolio_annualized_volatility = current_volatility
    untyped._consecutive_valid_release_checks = release_checks
    def reconcile_state(**_kwargs: object) -> SimpleNamespace:
        def is_critical(_conditions: object) -> bool:
            return False

        return SimpleNamespace(
            ok=True,
            conditions=(SimpleNamespace(value="OK"),),
            result_hash=HASH,
            is_critical=is_critical,
        )

    untyped._reconcile_state = reconcile_state
    return processor


def test_strategic_soft_stop_blocks_buys_without_typed_episode(
    repository_root: Path,
) -> None:
    processor = _processor(
        repository_root,
        valid_checks=0,
        include_episode=False,
    )

    def soft_baselines(**_kwargs: object) -> tuple[Decimal, Decimal]:
        return Decimal("1020"), Decimal("1020")

    cast(Any, processor)._risk_nav_baselines = soft_baselines
    gates = processor._prepare_strategic_risk_gates(
        cycle=_cycle(),
        calendar=_calendar(),
        created_at=NOW + timedelta(seconds=5),
        states={Q1ArmId.Q1_DET: _state()},
        quotes={
            "QQQ": DecisionQuote(
                symbol="QQQ",
                quote_id="quote-current",
                bid=Decimal("499"),
                ask=Decimal("501"),
                available_at=NOW + timedelta(seconds=1),
            )
        },
        source_manifest_hash=HASH,
    )

    gate = gates[Q1ArmId.Q1_DET]
    assert gate.episode_id is None
    assert gate.transition.new_episode is None
    assert gate.transition.new_events == ()
    assert gate.transition.effective_severity is RiskSeverity.SOFT_STOP
    assert gate.transition.block_new_buys is True
    assert gate.transition.cancel_pending_buys is True


def test_live_mirror_quote_failure_does_not_block_strategy_context(
    repository_root: Path,
) -> None:
    processor = object.__new__(Q1PaperCycleProcessor)
    untyped = cast(Any, processor)
    untyped._runtime = type(
        "RuntimeStub",
        (),
        {"config": load_q1_config_bundle(repository_root / "config")},
    )()
    live_state = Q1ArmState(
        arm_id=Q1ArmId.LIVE_MIRROR.value,
        initial_nav_usd=Decimal("1000"),
        settled_cash_usd=Decimal("500"),
        unsettled_receivables=(),
        positions={"NVDA": Decimal("1")},
        sequence=1,
        evaluation_anchor_id=None,
    )
    def read_live_state(*_args: object) -> Q1ArmState:
        return live_state

    untyped._read_state = read_live_state

    def missing_live_quote(**_kwargs: object) -> dict[str, DecisionQuote]:
        raise Q1CycleNotReady("Fresh quote missing for NVDA")

    untyped._fresh_decision_quotes = missing_live_quote
    strategy_state = _state()
    strategy_quote = DecisionQuote(
        symbol="QQQ",
        quote_id="quote-current",
        bid=Decimal("499"),
        ask=Decimal("501"),
        available_at=NOW,
    )

    states, quotes, skipped = processor._strategic_risk_context(
        run_id="q1-release-run",
        strategy_states={Q1ArmId.Q1_DET: strategy_state},
        strategy_quotes={"QQQ": strategy_quote},
        as_of=NOW,
    )

    assert states == {Q1ArmId.Q1_DET: strategy_state}
    assert quotes == {"QQQ": strategy_quote}
    assert skipped == (Q1ArmId.LIVE_MIRROR,)


def test_inherited_anchor_quotes_do_not_join_active_strategy_skew_bundle() -> None:
    processor = object.__new__(Q1PaperCycleProcessor)
    untyped = cast(Any, processor)
    calls: list[tuple[tuple[str, ...], bool]] = []
    anchor_quotes = {
        symbol: DecisionQuote(
            symbol=symbol,
            quote_id=f"anchor-{symbol}",
            bid=Decimal("99"),
            ask=Decimal("101"),
            available_at=NOW,
        )
        for symbol in ("NVDA", "QQQ", "SOXL")
    }
    active_quotes = {
        symbol: DecisionQuote(
            symbol=symbol,
            quote_id=f"active-{symbol}",
            bid=Decimal("199"),
            ask=Decimal("201"),
            available_at=NOW,
        )
        for symbol in ("QQQ", "SOXX")
    }

    def fresh_quotes(
        *,
        symbols: tuple[str, ...],
        as_of: datetime,
        observed_after: datetime | None = None,
        enforce_multi_symbol_skew: bool = True,
    ) -> dict[str, DecisionQuote]:
        del as_of, observed_after
        calls.append((symbols, enforce_multi_symbol_skew))
        return (
            dict(active_quotes)
            if enforce_multi_symbol_skew
            else {
                symbol: anchor_quotes[symbol]
                for symbol in symbols
            }
        )

    untyped._fresh_decision_quotes = fresh_quotes
    result = processor._strategic_quote_bundle(
        anchor_symbols=("SOXL", "NVDA"),
        as_of=NOW,
    )

    assert calls == [
        (("NVDA", "QQQ", "SOXL"), False),
        (("QQQ", "SOXX"), True),
    ]
    assert result["NVDA"].quote_id == "anchor-NVDA"
    assert result["SOXL"].quote_id == "anchor-SOXL"
    assert result["QQQ"].quote_id == "active-QQQ"
    assert result["SOXX"].quote_id == "active-SOXX"


def test_active_quote_skew_failure_preserves_anchor_and_qqq_benchmark() -> None:
    processor = object.__new__(Q1PaperCycleProcessor)
    untyped = cast(Any, processor)
    anchor_quotes = {
        symbol: DecisionQuote(
            symbol=symbol,
            quote_id=f"anchor-{symbol}",
            bid=Decimal("99"),
            ask=Decimal("101"),
            available_at=NOW,
        )
        for symbol in ("NVDA", "QQQ", "SOXL")
    }

    def fresh_quotes(
        *,
        symbols: tuple[str, ...],
        as_of: datetime,
        observed_after: datetime | None = None,
        enforce_multi_symbol_skew: bool = True,
    ) -> dict[str, DecisionQuote]:
        del as_of, observed_after
        if enforce_multi_symbol_skew:
            raise Q1CycleNotReady(
                "Decision quote bundle exceeds maximum skew"
            )
        return {
            symbol: anchor_quotes[symbol]
            for symbol in symbols
        }

    untyped._fresh_decision_quotes = fresh_quotes
    result = processor._strategic_quote_bundle(
        anchor_symbols=("SOXL", "NVDA"),
        as_of=NOW,
    )

    assert set(result) == {"NVDA", "QQQ", "SOXL"}
    assert result["QQQ"].quote_id == "anchor-QQQ"
    assert "SOXX" not in result


def test_next_session_strategic_releases_after_two_valid_checks(
    repository_root: Path,
) -> None:
    processor = _processor(repository_root, valid_checks=2)
    gates = processor._prepare_strategic_risk_gates(
        cycle=_cycle(),
        calendar=_calendar(),
        created_at=NOW + timedelta(seconds=5),
        states={Q1ArmId.Q1_DET: _state()},
        quotes={
            "QQQ": DecisionQuote(
                symbol="QQQ",
                quote_id="quote-current",
                bid=Decimal("499"),
                ask=Decimal("501"),
                available_at=NOW + timedelta(seconds=1),
            )
        },
        source_manifest_hash=HASH,
    )

    gate = gates[Q1ArmId.Q1_DET]
    assert gate.released is True
    assert gate.consecutive_valid_release_checks == 2
    assert gate.transition.active_episode is None
    assert tuple(
        event.event_type for event in gate.transition.new_events
    ) == (RiskEpisodeEventType.RELEASE,)
    assert gate.transition.release_allows_automatic_buys is False


def test_strategic_keeps_latch_when_release_checks_are_insufficient(
    repository_root: Path,
) -> None:
    processor = _processor(repository_root, valid_checks=1)
    gates = processor._prepare_strategic_risk_gates(
        cycle=_cycle(),
        calendar=_calendar(),
        created_at=NOW + timedelta(seconds=5),
        states={Q1ArmId.Q1_DET: _state()},
        quotes={
            "QQQ": DecisionQuote(
                symbol="QQQ",
                quote_id="quote-current",
                bid=Decimal("499"),
                ask=Decimal("501"),
                available_at=NOW + timedelta(seconds=1),
            )
        },
        source_manifest_hash=HASH,
    )

    gate = gates[Q1ArmId.Q1_DET]
    assert gate.released is False
    assert gate.transition.active_episode is not None
    assert gate.transition.executable_residual_targets[0].target_quantity == (
        Decimal("0.5")
    )
    assert gate.transition.block_new_buys is True


def test_general_and_latched_weights_preserve_cash_and_turnover() -> None:
    state = Q1ArmState(
        arm_id=Q1ArmId.LIVE_MIRROR.value,
        initial_nav_usd=Decimal("300"),
        settled_cash_usd=Decimal("100"),
        unsettled_receivables=(),
        positions={"QQQ": Decimal("1"), "SOXX": Decimal("2")},
        sequence=1,
        evaluation_anchor_id=None,
    )
    prices = {"QQQ": Decimal("100"), "SOXX": Decimal("50")}
    current = _general_weights(state, prices)
    target = _target_quantity_weights(
        state=state,
        target_quantities={
            "QQQ": Decimal("0.5"),
            "SOXX": Decimal("1"),
        },
        prices=prices,
    )

    assert sum(current.values(), Decimal("0")) == 1
    assert sum(target.values(), Decimal("0")) == 1
    tolerance = Decimal("0.000000000000000000000000001")
    assert abs(
        target["USD_CASH"] - Decimal("2") / Decimal("3")
    ) < tolerance
    assert abs(
        _one_way_turnover(current, target)
        - Decimal("1") / Decimal("3")
    ) < tolerance


def test_latched_target_rejects_absent_symbol() -> None:
    with pytest.raises(Q1CycleError, match="absent positions"):
        _target_quantity_weights(
            state=_state(),
            target_quantities={"SOXX": Decimal("0")},
            prices={"QQQ": Decimal("500")},
        )


def test_release_check_counter_uses_consecutive_current_session_navs(
    sqlite_database: tuple[str, Engine, sessionmaker[Session]],
) -> None:
    _database_url, _engine, factory = sqlite_database
    with factory.begin() as session:
        session.add(
            RunRow(
                run_id="q1-release-run",
                mode="PAPER",
                experiment_version="q1_math_core_v1",
                config_manifest_hash=HASH,
                code_commit="test-code",
                started_at=NOW - timedelta(days=2),
                ended_at=None,
                status="RUNNING",
                result_manifest=None,
                result_hash=None,
            )
        )
        session.flush()
        for identity, as_of, calendar_id, valid in (
            (
                "previous-session",
                NOW - timedelta(days=1),
                "calendar-previous",
                True,
            ),
            (
                "current-0945",
                NOW - timedelta(minutes=15),
                "calendar-current",
                True,
            ),
            (
                "current-1000",
                NOW,
                "calendar-current",
                True,
            ),
        ):
            session.add(
                NavSnapshotRow(
                    nav_snapshot_id=identity,
                    run_id="q1-release-run",
                    arm_id=Q1ArmId.Q1_DET.value,
                    source_cycle_id=None,
                    quote_manifest_hash=HASH,
                    algorithm_version="q1_math_core_v1",
                    config_manifest_hash=HASH,
                    code_version="test-code",
                    model_version="test-model",
                    source_manifest_hash=HASH,
                    as_of=as_of,
                    nav_usd=Decimal("1000"),
                    payload_json={
                        "calendar_session_id": calendar_id,
                        "release_condition_valid": valid,
                        "reconciliation_ok": True,
                    },
                )
            )

    processor = object.__new__(Q1PaperCycleProcessor)
    cast(Any, processor)._session_factory = factory
    assert processor._consecutive_valid_release_checks(
        run_id="q1-release-run",
        arm_id=Q1ArmId.Q1_DET.value,
        calendar_session_id="calendar-current",
        as_of=NOW + timedelta(seconds=5),
    ) == 2

    with factory.begin() as session:
        session.add(
            NavSnapshotRow(
                nav_snapshot_id="current-invalid",
                run_id="q1-release-run",
                arm_id=Q1ArmId.Q1_DET.value,
                source_cycle_id=None,
                quote_manifest_hash=HASH,
                algorithm_version="q1_math_core_v1",
                config_manifest_hash=HASH,
                code_version="test-code",
                model_version="test-model",
                source_manifest_hash=HASH,
                as_of=NOW + timedelta(seconds=1),
                nav_usd=Decimal("1000"),
                payload_json={
                    "calendar_session_id": "calendar-current",
                    "release_condition_valid": False,
                    "reconciliation_ok": True,
                },
            )
        )
    assert processor._consecutive_valid_release_checks(
        run_id="q1-release-run",
        arm_id=Q1ArmId.Q1_DET.value,
        calendar_session_id="calendar-current",
        as_of=NOW + timedelta(seconds=5),
    ) == 0
