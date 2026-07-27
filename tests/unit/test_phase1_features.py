from __future__ import annotations

import math
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from trading.domain.contracts import MarketBar, MarketQuote
from trading.domain.enums import MarketDataSourceKind
from trading.features.models import (
    AdjustedPriceObservation,
    FeatureBuildContext,
    IndexMembership,
    PortfolioWeightSnapshot,
    ScheduledEventWindow,
)
from trading.features.pit import select_session_marks
from trading.features.r1 import R1FeatureParameters, build_r1_features
from trading.features.t1 import T1FeatureParameters, build_t1_features
from trading.features.x1 import build_x1_features

NEW_YORK = ZoneInfo("America/New_York")


def test_session_mark_selection_rejects_future_sessions_and_late_revisions() -> None:
    current_session = date(2026, 7, 27)
    cutoff = _at_et(current_session, time(15, 44))
    current = AdjustedPriceObservation(
        source_record_id="current",
        symbol="QQQ",
        session_date=current_session,
        event_time=cutoff,
        available_at=cutoff,
        adjusted_price=Decimal("100"),
    )
    late_revision = current.model_copy(
        update={
            "source_record_id": "late",
            "available_at": cutoff + timedelta(seconds=1),
            "adjusted_price": Decimal("999"),
        }
    )
    future_session = current.model_copy(
        update={
            "source_record_id": "future",
            "session_date": current_session + timedelta(days=1),
            "event_time": cutoff + timedelta(days=1),
            "available_at": cutoff + timedelta(days=1),
            "adjusted_price": Decimal("999"),
        }
    )

    panel = select_session_marks(
        [current, late_revision, future_session],
        reference_cutoff=cutoff,
    )

    assert panel["QQQ"] == {current_session: current}


def test_t1_is_point_in_time_and_requires_historical_membership() -> None:
    sessions = _weekdays(date(2024, 12, 2), 380)
    symbols = ("SOXX", "QQQ", "C1", "C2", "C3", "C4", "C5")
    slopes = {
        "SOXX": 0.0010,
        "QQQ": 0.00055,
        "C1": 0.0013,
        "C2": 0.0011,
        "C3": 0.0009,
        "C4": 0.0007,
        "C5": 0.0005,
    }
    observations = _adjusted_observations(sessions, symbols, slopes)
    memberships = [
        IndexMembership(
            source_record_id=f"membership-{symbol}",
            universe_id="SOXX",
            symbol=symbol,
            effective_from=sessions[0],
            available_at=_at_et(sessions[0] - timedelta(days=1), time(12, 0)),
        )
        for symbol in symbols
        if symbol.startswith("C")
    ]
    context = _feature_context(sessions[-1], "t1_features_v1")
    parameters = T1FeatureParameters(
        min_constituents=5,
        min_constituent_coverage=Decimal("1"),
    )

    result = build_t1_features(
        context=context,
        prices=observations,
        memberships=memberships,
        parameters=parameters,
    )

    assert result.ready
    assert result.snapshot is not None
    values = {value.name: value.value for value in result.snapshot.values}
    assert -3 <= values["z_rs_fast"] <= 3
    assert Decimal("0.3") <= Decimal(str(values["beta_60"])) <= Decimal("2.0")

    future = observations[-1].model_copy(
        update={
            "source_record_id": "future-revision",
            "adjusted_price": Decimal("99999"),
            "available_at": context.decision_time + timedelta(minutes=2),
        }
    )
    repeated = build_t1_features(
        context=context,
        prices=[*observations, future],
        memberships=memberships,
        parameters=parameters,
    )
    assert repeated.snapshot == result.snapshot

    blocked = build_t1_features(
        context=context,
        prices=observations,
        memberships=[],
        parameters=parameters,
    )
    assert not blocked.ready
    assert blocked.reason_codes == ("T1_PIT_MEMBERSHIP_REQUIRED",)


def test_x1_builds_capped_zero_sum_active_delta_without_future_data() -> None:
    sessions = _weekdays(date(2026, 2, 2), 100)
    assets = ("SPY", "QQQ", "IWM", "SOXX", "XLK", "HYG", "TLT", "GLD")
    slopes = {
        "SPY": 0.0005,
        "QQQ": 0.0009,
        "IWM": 0.0002,
        "SOXX": 0.0014,
        "XLK": 0.0010,
        "HYG": 0.00015,
        "TLT": -0.0002,
        "GLD": 0.00035,
        "USD_CASH": 0.00005,
    }
    observations = _adjusted_observations(
        sessions,
        (*assets, "USD_CASH"),
        slopes,
    )
    context = _feature_context(sessions[-1], "x1_features_v1")
    core = PortfolioWeightSnapshot(
        snapshot_id="core-1",
        portfolio_id="B0_VOL_V1",
        as_of=context.data_available_cutoff - timedelta(minutes=1),
        available_at=context.data_available_cutoff,
        weights={"QQQ": Decimal("0.60"), "USD_CASH": Decimal("0.40")},
    )
    previous = PortfolioWeightSnapshot(
        snapshot_id="x1-previous",
        portfolio_id="X1_TARGET",
        as_of=context.data_available_cutoff - timedelta(minutes=1),
        available_at=context.data_available_cutoff,
        weights={"QQQ": Decimal("0.60"), "USD_CASH": Decimal("0.40")},
    )

    result = build_x1_features(
        context=context,
        prices=observations,
        core_portfolio=core,
        previous_target=previous,
    )

    assert result.ready
    assert result.snapshot is not None
    values = {value.name: Decimal(str(value.value)) for value in result.snapshot.values}
    targets = {symbol: values[f"target.{symbol}"] for symbol in (*assets, "USD_CASH")}
    deltas = {symbol: values[f"active_delta.{symbol}"] for symbol in (*assets, "USD_CASH")}
    assert abs(sum(targets.values(), Decimal("0")) - Decimal("1")) < Decimal("1e-10")
    assert abs(sum(deltas.values(), Decimal("0"))) < Decimal("1e-10")
    assert max(targets[symbol] for symbol in assets) <= Decimal("0.3500000001")
    assert targets["QQQ"] + targets["SOXX"] + targets["XLK"] <= Decimal("0.7000000001")

    future = observations[-1].model_copy(
        update={
            "source_record_id": "future-x1",
            "adjusted_price": Decimal("0.01"),
            "available_at": context.decision_time + timedelta(hours=1),
        }
    )
    repeated = build_x1_features(
        context=context,
        prices=[*observations, future],
        core_portfolio=core,
        previous_target=previous,
    )
    assert repeated.snapshot == result.snapshot

    future_core = core.model_copy(
        update={"available_at": context.decision_time + timedelta(seconds=1)}
    )
    blocked = build_x1_features(
        context=context,
        prices=observations,
        core_portfolio=future_core,
        previous_target=previous,
    )
    assert blocked.reason_codes == ("X1_PORTFOLIO_SNAPSHOT_FROM_FUTURE",)


def test_r1_uses_quotes_and_known_event_windows_for_entry_gate() -> None:
    sessions = _weekdays(date(2026, 6, 1), 7)
    symbols = ("SMH", "SPY", "QQQ", "SOXX", "TLT")
    bars = _r1_bars(sessions, symbols)
    quotes = _r1_quotes(sessions, "SMH")
    context = FeatureBuildContext(
        decision_time=_at_et(sessions[-1], time(11, 0)),
        data_available_cutoff=_at_et(sessions[-1], time(11, 0)),
        created_at=_at_et(sessions[-1], time(11, 0, 1)),
        feature_set_version="r1_features_v1",
    )
    parameters = R1FeatureParameters(
        targets=("SMH",),
        training_sessions=6,
        quote_history_sessions=3,
        volume_history_sessions=3,
        min_regression_observations=50,
    )

    result = build_r1_features(
        context=context,
        bars=bars,
        quotes=quotes,
        scheduled_events=[],
        parameters=parameters,
    )[0]

    assert result.ready
    assert result.snapshot is not None
    values = {value.name: Decimal(str(value.value)) for value in result.snapshot.values}
    assert values["raw_signal"] > 0
    assert values["spread_ratio"] <= parameters.max_spread_ratio

    last_smh_bar = next(
        bar
        for bar in reversed(bars)
        if bar.symbol == "SMH" and bar.event_time.astimezone(NEW_YORK).date() == sessions[-1]
    )
    late_revision = last_smh_bar.model_copy(
        update={
            "bar_id": "late-r1-revision",
            "close": Decimal("1"),
            "available_at": context.decision_time + timedelta(seconds=1),
            "ingested_at": context.decision_time + timedelta(seconds=1),
        }
    )
    repeated = build_r1_features(
        context=context,
        bars=[*bars, late_revision],
        quotes=quotes,
        scheduled_events=[],
        parameters=parameters,
    )[0]
    assert repeated.snapshot == result.snapshot

    event = ScheduledEventWindow(
        source_record_id="calendar-event",
        event_type="FOMC",
        starts_at=context.decision_time - timedelta(minutes=30),
        ends_at=context.decision_time + timedelta(minutes=30),
        available_at=context.decision_time - timedelta(days=7),
        affected_symbols=(),
    )
    blocked_entry = build_r1_features(
        context=context,
        bars=bars,
        quotes=quotes,
        scheduled_events=[event],
        parameters=parameters,
    )[0]
    assert blocked_entry.snapshot is not None
    blocked_values = {
        value.name: Decimal(str(value.value)) for value in blocked_entry.snapshot.values
    }
    assert blocked_values["event_blocked"] == 1
    assert blocked_values["eligible"] == 0


def _adjusted_observations(
    sessions: list[date],
    symbols: tuple[str, ...],
    slopes: dict[str, float],
) -> list[AdjustedPriceObservation]:
    observations: list[AdjustedPriceObservation] = []
    for symbol_index, symbol in enumerate(symbols):
        price = 80.0 + symbol_index * 11
        for index, session in enumerate(sessions):
            wave = 0.0008 * math.sin(index * (0.11 + symbol_index * 0.007))
            pulse = 0.0003 * math.cos(index * (0.037 + symbol_index * 0.003))
            price *= 1 + slopes[symbol] + wave + pulse
            instant = _at_et(session, time(15, 44))
            observations.append(
                AdjustedPriceObservation(
                    source_record_id=f"{symbol}-{session.isoformat()}",
                    symbol=symbol,
                    session_date=session,
                    event_time=instant,
                    available_at=instant,
                    adjusted_price=Decimal(str(round(price, 8))),
                )
            )
    return observations


def _r1_bars(sessions: list[date], symbols: tuple[str, ...]) -> list[MarketBar]:
    bars: list[MarketBar] = []
    for session_index, session in enumerate(sessions):
        prices = {symbol: Decimal("100") + Decimal(session_index) for symbol in symbols}
        for interval in range(18):
            local_time = datetime.combine(session, time(9, 30), tzinfo=NEW_YORK) + timedelta(
                minutes=interval * 5
            )
            for symbol_index, symbol in enumerate(symbols):
                factor_return = Decimal(
                    str(
                        0.00015
                        + 0.00011 * math.sin((interval + 1) * (symbol_index + 2))
                        + 0.00004 * math.cos((session_index + 1) * (symbol_index + 1))
                    )
                )
                if symbol == "SMH":
                    factor_return = Decimal(
                        str(0.00018 + 0.00007 * math.sin(interval * 1.7 + session_index))
                    )
                    if session == sessions[-1] and interval >= 6:
                        factor_return -= Decimal("0.0012")
                prices[symbol] *= Decimal("1") + factor_return
                event_time = local_time.astimezone(UTC)
                available_at = (local_time + timedelta(minutes=5)).astimezone(UTC)
                close = prices[symbol]
                volume = Decimal(100 + session_index * 10 + symbol_index)
                bars.append(
                    MarketBar(
                        bar_id=f"bar-{symbol}-{session}-{interval}",
                        provider="test",
                        feed="iex",
                        symbol=symbol,
                        timeframe="5Min",
                        event_time=event_time,
                        provider_timestamp=event_time.isoformat(),
                        available_at=available_at,
                        ingested_at=available_at,
                        source_kind=MarketDataSourceKind.REST_BACKFILL,
                        open=close,
                        high=close,
                        low=close,
                        close=close,
                        volume=volume,
                        vwap=close,
                        trade_count=1,
                        request_id=None,
                        payload_hash=f"hash-{symbol}-{session}-{interval}",
                        raw_object_uri=None,
                        payload={},
                    )
                )
    return bars


def _r1_quotes(sessions: list[date], symbol: str) -> list[MarketQuote]:
    quotes: list[MarketQuote] = []
    spread_dollars = (
        Decimal("0.020"),
        Decimal("0.022"),
        Decimal("0.018"),
        Decimal("0.021"),
        Decimal("0.019"),
        Decimal("0.020"),
        Decimal("0.020"),
    )
    for index, session in enumerate(sessions):
        instant = _at_et(session, time(10, 54))
        midpoint = Decimal("100") + Decimal(index)
        spread = spread_dollars[index]
        quotes.append(
            MarketQuote(
                quote_id=f"quote-{symbol}-{session}",
                provider="test",
                feed="iex",
                symbol=symbol,
                event_time=instant,
                provider_timestamp=instant.isoformat(),
                available_at=instant,
                ingested_at=instant,
                source_kind=MarketDataSourceKind.STREAM_QUOTE,
                bid_exchange="V",
                bid_price=midpoint - spread / Decimal("2"),
                bid_size_round_lots=10,
                ask_exchange="V",
                ask_price=midpoint + spread / Decimal("2"),
                ask_size_round_lots=10,
                conditions=[],
                tape="C",
                payload_hash=f"quote-hash-{session}",
                raw_object_uri=None,
                payload={},
            )
        )
    return quotes


def _feature_context(session: date, version: str) -> FeatureBuildContext:
    return FeatureBuildContext(
        decision_time=_at_et(session, time(15, 45)),
        data_available_cutoff=_at_et(session, time(15, 44)),
        created_at=_at_et(session, time(15, 45, 1)),
        feature_set_version=version,
    )


def _at_et(session: date, clock: time) -> datetime:
    return datetime.combine(session, clock, tzinfo=NEW_YORK).astimezone(UTC)


def _weekdays(start: date, count: int) -> list[date]:
    sessions: list[date] = []
    current = start
    while len(sessions) < count:
        if current.weekday() < 5:
            sessions.append(current)
        current += timedelta(days=1)
    return sessions
