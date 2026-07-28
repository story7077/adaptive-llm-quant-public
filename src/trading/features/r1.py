from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from decimal import Decimal
from itertools import pairwise
from zoneinfo import ZoneInfo

from trading.domain.contracts import MarketBar, MarketQuote
from trading.features.models import (
    FeatureBuildContext,
    FeatureBuildResult,
    ScheduledEventWindow,
    blocked_result,
    feature_snapshot,
)
from trading.features.statistics import (
    StatisticsError,
    mean,
    median,
    ols_coefficients,
    sample_std,
    simple_return,
)

STRATEGY_ID = "R1"
FEATURE_CODE_VERSION = "r1_features_v1"
NEW_YORK = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class R1FeatureParameters:
    targets: tuple[str, ...] = ("QQQ", "SOXX", "SMH", "XLK")
    factors: tuple[str, ...] = ("SPY", "QQQ", "SOXX", "TLT")
    training_sessions: int = 60
    quote_history_sessions: int = 20
    volume_history_sessions: int = 20
    return_minutes: int = 60
    bar_minutes: int = 5
    signal_floor: Decimal = Decimal("2.0")
    max_spread_ratio: Decimal = Decimal("1.5")
    min_volume_z: Decimal = Decimal("0.5")
    max_volume_z: Decimal = Decimal("4.0")
    common_shock_z: Decimal = Decimal("1.5")
    quote_clock_tolerance_minutes: int = 10
    min_regression_observations: int = 300

    def __post_init__(self) -> None:
        if not self.targets or not self.factors:
            raise ValueError("R1 targets and factors cannot be empty")
        positive = (
            self.training_sessions,
            self.quote_history_sessions,
            self.volume_history_sessions,
            self.return_minutes,
            self.bar_minutes,
            self.quote_clock_tolerance_minutes,
            self.min_regression_observations,
        )
        if any(value <= 0 for value in positive):
            raise ValueError("R1 history and interval parameters must be positive")
        if self.bar_minutes != 5:
            raise ValueError("R1 beta training requires 5-minute bars")
        if self.return_minutes % self.bar_minutes:
            raise ValueError("R1 return_minutes must be divisible by bar_minutes")


@dataclass(frozen=True, slots=True)
class _BarPoint:
    symbol: str
    session_date: date
    bucket_time: time
    interval_minutes: int
    close: Decimal
    volume: Decimal
    source_ids: tuple[str, ...]


def build_r1_features(
    *,
    context: FeatureBuildContext,
    bars: list[MarketBar],
    quotes: list[MarketQuote],
    scheduled_events: list[ScheduledEventWindow],
    open_position_symbols: set[str] | frozenset[str] = frozenset(),
    parameters: R1FeatureParameters | None = None,
) -> list[FeatureBuildResult]:
    """Build one safe R1 feature result for each configured liquid ETF."""

    parameters = parameters or R1FeatureParameters()
    if context.feature_set_version != FEATURE_CODE_VERSION:
        return [
            blocked_result(STRATEGY_ID, symbol, "R1_FEATURE_VERSION_MISMATCH")
            for symbol in parameters.targets
        ]
    points = _five_minute_points(
        bars,
        cutoff=context.data_available_cutoff,
        bar_minutes=parameters.bar_minutes,
    )
    by_symbol_date: dict[str, dict[date, list[_BarPoint]]] = defaultdict(dict)
    for symbol, by_date in points.items():
        by_symbol_date[symbol] = {
            session: sorted(items, key=lambda item: item.bucket_time)
            for session, items in by_date.items()
        }

    return [
        _build_symbol_features(
            symbol=symbol,
            context=context,
            by_symbol_date=by_symbol_date,
            quotes=quotes,
            scheduled_events=scheduled_events,
            open_position_symbols=open_position_symbols,
            parameters=parameters,
        )
        for symbol in parameters.targets
    ]


def _build_symbol_features(
    *,
    symbol: str,
    context: FeatureBuildContext,
    by_symbol_date: dict[str, dict[date, list[_BarPoint]]],
    quotes: list[MarketQuote],
    scheduled_events: list[ScheduledEventWindow],
    open_position_symbols: set[str] | frozenset[str],
    parameters: R1FeatureParameters,
) -> FeatureBuildResult:
    local_cutoff = context.data_available_cutoff.astimezone(NEW_YORK)
    current_session = local_cutoff.date()
    factors = tuple(factor for factor in parameters.factors if factor != symbol)
    required_symbols = (symbol, *factors)
    if any(item not in by_symbol_date for item in required_symbols):
        return blocked_result(STRATEGY_ID, symbol, "R1_FACTOR_HISTORY_REQUIRED")

    prior_sessions = sorted(
        _set_intersection(
            set(by_symbol_date[item]) - {current_session} for item in required_symbols
        )
    )
    if len(prior_sessions) < parameters.training_sessions:
        return blocked_result(STRATEGY_ID, symbol, "R1_60_SESSION_HISTORY_REQUIRED")
    prior_sessions = prior_sessions[-parameters.training_sessions :]

    current_points: dict[str, list[_BarPoint]] = {}
    for item in required_symbols:
        series = by_symbol_date[item].get(current_session, [])
        if not series:
            return blocked_result(STRATEGY_ID, symbol, "R1_CURRENT_INTRADAY_BARS_REQUIRED")
        current_points[item] = series
    common_current_times = _set_intersection(
        {point.bucket_time for point in current_points[item]} for item in required_symbols
    )
    if not common_current_times:
        return blocked_result(STRATEGY_ID, symbol, "R1_UNALIGNED_CURRENT_BARS")
    end_clock = max(common_current_times)
    intervals = parameters.return_minutes // parameters.bar_minutes
    source_ids: set[str] = set()

    try:
        current_returns = {
            item: _window_return(
                current_points[item],
                end_clock=end_clock,
                intervals=intervals,
                source_ids=source_ids,
            )
            for item in required_symbols
        }
        training_rows = _aligned_training_returns(
            by_symbol_date=by_symbol_date,
            symbols=required_symbols,
            sessions=prior_sessions,
            end_clock=end_clock,
            source_ids=source_ids,
        )
        if len(training_rows) < parameters.min_regression_observations:
            raise StatisticsError("insufficient aligned regression observations")
        intercept, betas = ols_coefficients(
            [row[symbol] for row in training_rows],
            [[row[factor] for row in training_rows] for factor in factors],
        )
        current_residual = current_returns[symbol] - sum(
            (beta * current_returns[factor] for beta, factor in zip(betas, factors, strict=True)),
            intercept * Decimal(intervals),
        )

        historical_residuals: list[Decimal] = []
        historical_target_returns: list[Decimal] = []
        historical_factor_returns: dict[str, list[Decimal]] = {
            factor: [] for factor in ("SPY", "QQQ") if factor in by_symbol_date
        }
        historical_volumes: list[Decimal] = []
        for session in prior_sessions:
            session_returns: dict[str, Decimal] = {}
            for item in required_symbols:
                session_returns[item] = _window_return(
                    by_symbol_date[item][session],
                    end_clock=end_clock,
                    intervals=intervals,
                    source_ids=source_ids,
                )
            historical_residuals.append(
                session_returns[symbol]
                - sum(
                    (
                        beta * session_returns[factor]
                        for beta, factor in zip(betas, factors, strict=True)
                    ),
                    intercept * Decimal(intervals),
                )
            )
            historical_target_returns.append(session_returns[symbol])
            for shock_symbol in historical_factor_returns:
                if shock_symbol in session_returns:
                    historical_factor_returns[shock_symbol].append(session_returns[shock_symbol])
                else:
                    historical_factor_returns[shock_symbol].append(
                        _window_return(
                            by_symbol_date[shock_symbol][session],
                            end_clock=end_clock,
                            intervals=intervals,
                            source_ids=source_ids,
                        )
                    )
            historical_volumes.append(
                _window_volume(
                    by_symbol_date[symbol][session],
                    end_clock=end_clock,
                    intervals=intervals,
                    source_ids=source_ids,
                )
            )

        residual_sigma = sample_std(historical_residuals)
        raw_signal = -current_residual / residual_sigma
        current_volume = _window_volume(
            current_points[symbol],
            end_clock=end_clock,
            intervals=intervals,
            source_ids=source_ids,
        )
        volume_history = historical_volumes[-parameters.volume_history_sessions :]
        volume_z = (current_volume - mean(volume_history)) / sample_std(volume_history)

        common_shock = Decimal("0")
        for shock_symbol in ("SPY", "QQQ"):
            if shock_symbol not in by_symbol_date:
                raise StatisticsError(f"missing common shock series {shock_symbol}")
            current_shock_return = (
                current_returns[shock_symbol]
                if shock_symbol in current_returns
                else _window_return(
                    by_symbol_date[shock_symbol][current_session],
                    end_clock=end_clock,
                    intervals=intervals,
                    source_ids=source_ids,
                )
            )
            history = historical_factor_returns.get(shock_symbol)
            if history is None or len(history) < parameters.training_sessions:
                history = [
                    _window_return(
                        by_symbol_date[shock_symbol][session],
                        end_clock=end_clock,
                        intervals=intervals,
                        source_ids=source_ids,
                    )
                    for session in prior_sessions
                ]
            shock_z = abs((current_shock_return - mean(history)) / sample_std(history))
            common_shock = max(common_shock, shock_z)
    except (KeyError, StatisticsError):
        return blocked_result(STRATEGY_ID, symbol, "R1_UNSTABLE_OR_INCOMPLETE_RETURNS")

    quote_result = _spread_features(
        symbol=symbol,
        quotes=quotes,
        cutoff=context.data_available_cutoff,
        end_clock=end_clock,
        history_sessions=parameters.quote_history_sessions,
        tolerance_minutes=parameters.quote_clock_tolerance_minutes,
    )
    if quote_result is None:
        return blocked_result(STRATEGY_ID, symbol, "R1_QUOTE_HISTORY_REQUIRED")
    current_spread_bps, median_spread_bps, quote_ids = quote_result
    source_ids.update(quote_ids)
    if median_spread_bps <= 0:
        return blocked_result(STRATEGY_ID, symbol, "R1_INVALID_QUOTE_HISTORY")
    spread_ratio = current_spread_bps / median_spread_bps

    event_blocked = any(
        event.available_at <= context.data_available_cutoff
        and event.blocks(symbol, context.decision_time)
        for event in scheduled_events
    )
    source_ids.update(
        event.source_record_id
        for event in scheduled_events
        if event.available_at <= context.data_available_cutoff
        and event.blocks(symbol, context.decision_time)
    )
    existing_position = symbol in open_position_symbols
    horizon_vol = residual_sigma * Decimal("2")
    eligible = (
        raw_signal >= parameters.signal_floor
        and spread_ratio <= parameters.max_spread_ratio
        and parameters.min_volume_z <= volume_z <= parameters.max_volume_z
        and common_shock < parameters.common_shock_z
        and not event_blocked
        and not existing_position
    )

    lineage = sorted(source_ids)
    values: list[tuple[str, Decimal, str, list[str]]] = [
        ("raw_signal", raw_signal, "z_score", lineage),
        ("residual_return_60m", current_residual, "return", lineage),
        ("same_clock_residual_sigma", residual_sigma, "return", lineage),
        ("horizon_vol", horizon_vol, "return_volatility", lineage),
        ("spread_bps", current_spread_bps, "basis_points", quote_ids),
        ("spread_ratio", spread_ratio, "ratio", quote_ids),
        ("volume_z", volume_z, "z_score", lineage),
        ("common_shock_z", common_shock, "z_score", lineage),
        ("event_blocked", Decimal(int(event_blocked)), "boolean", lineage),
        ("existing_position", Decimal(int(existing_position)), "boolean", []),
        ("eligible", Decimal(int(eligible)), "boolean", lineage),
    ]
    manifest = {
        "strategy_id": STRATEGY_ID,
        "symbol": symbol,
        "feature_code_version": FEATURE_CODE_VERSION,
        "parameters": asdict(parameters),
        "source_record_ids": lineage,
        "current_session": current_session.isoformat(),
        "end_clock": end_clock.isoformat(),
        "factors": factors,
    }
    return feature_snapshot(
        strategy_id=STRATEGY_ID,
        symbol=symbol,
        context=context,
        feature_code_version=FEATURE_CODE_VERSION,
        values=values,
        manifest=manifest,
    )


def _five_minute_points(
    bars: list[MarketBar],
    *,
    cutoff: datetime,
    bar_minutes: int,
) -> dict[str, dict[date, list[_BarPoint]]]:
    selected: dict[tuple[str, datetime], MarketBar] = {}
    for bar in bars:
        if (
            bar.timeframe not in {"1Min", "5Min"}
            or bar.available_at > cutoff
            or bar.event_time > cutoff
        ):
            continue
        key = (bar.symbol, bar.event_time)
        current = selected.get(key)
        if current is None or (bar.available_at, bar.bar_id) > (
            current.available_at,
            current.bar_id,
        ):
            selected[key] = bar

    buckets: dict[
        tuple[str, date, time],
        list[MarketBar],
    ] = defaultdict(list)
    for bar in selected.values():
        local = bar.event_time.astimezone(NEW_YORK)
        bucket_minute = local.minute - local.minute % bar_minutes
        bucket_time = (
            local.replace(
                minute=bucket_minute,
                second=0,
                microsecond=0,
            )
            .time()
            .replace(tzinfo=None)
        )
        buckets[(bar.symbol, local.date(), bucket_time)].append(bar)

    result: dict[str, dict[date, list[_BarPoint]]] = defaultdict(lambda: defaultdict(list))
    for (symbol, session, bucket_time), bucket_bars in buckets.items():
        minute_bars = [bar for bar in bucket_bars if bar.timeframe == "1Min"]
        if minute_bars:
            if len(minute_bars) != bar_minutes:
                continue
            ordered = sorted(minute_bars, key=lambda item: item.event_time)
        else:
            five_minute_bars = [bar for bar in bucket_bars if bar.timeframe == "5Min"]
            if len(five_minute_bars) != 1:
                continue
            ordered = five_minute_bars
        result[symbol][session].append(
            _BarPoint(
                symbol=symbol,
                session_date=session,
                bucket_time=bucket_time,
                interval_minutes=bar_minutes,
                close=ordered[-1].close,
                volume=sum((item.volume for item in ordered), Decimal("0")),
                source_ids=tuple(item.bar_id for item in ordered),
            )
        )
    return result


def _window_points(
    points: list[_BarPoint],
    *,
    end_clock: time,
    intervals: int,
) -> list[_BarPoint]:
    eligible = [point for point in points if point.bucket_time <= end_clock]
    if not eligible or eligible[-1].bucket_time != end_clock or len(eligible) <= intervals:
        raise StatisticsError("intraday return window is incomplete")
    selected = eligible[-(intervals + 1) :]
    if any(
        _clock_minutes(current.bucket_time) - _clock_minutes(prior.bucket_time)
        != current.interval_minutes
        for prior, current in pairwise(selected)
    ):
        raise StatisticsError("intraday return window has missing bars")
    return selected


def _window_return(
    points: list[_BarPoint],
    *,
    end_clock: time,
    intervals: int,
    source_ids: set[str],
) -> Decimal:
    selected = _window_points(points, end_clock=end_clock, intervals=intervals)
    source_ids.update(source_id for point in selected for source_id in point.source_ids)
    return simple_return(selected[0].close, selected[-1].close)


def _window_volume(
    points: list[_BarPoint],
    *,
    end_clock: time,
    intervals: int,
    source_ids: set[str],
) -> Decimal:
    selected = _window_points(points, end_clock=end_clock, intervals=intervals)
    interval_points = selected[1:]
    source_ids.update(source_id for point in interval_points for source_id in point.source_ids)
    return sum((point.volume for point in interval_points), Decimal("0"))


def _aligned_training_returns(
    *,
    by_symbol_date: dict[str, dict[date, list[_BarPoint]]],
    symbols: tuple[str, ...],
    sessions: list[date],
    end_clock: time,
    source_ids: set[str],
) -> list[dict[str, Decimal]]:
    rows: list[dict[str, Decimal]] = []
    for session in sessions:
        lookup = {
            symbol: {
                point.bucket_time: point
                for point in by_symbol_date[symbol][session]
                if point.bucket_time <= end_clock
            }
            for symbol in symbols
        }
        clocks = sorted(_set_intersection(set(lookup[symbol]) for symbol in symbols))
        for prior_clock, clock in pairwise(clocks):
            first_point = lookup[symbols[0]][clock]
            if _clock_minutes(clock) - _clock_minutes(prior_clock) != first_point.interval_minutes:
                continue
            row: dict[str, Decimal] = {}
            for symbol in symbols:
                prior = lookup[symbol][prior_clock]
                current = lookup[symbol][clock]
                source_ids.update((*prior.source_ids, *current.source_ids))
                row[symbol] = simple_return(prior.close, current.close)
            rows.append(row)
    return rows


def _spread_features(
    *,
    symbol: str,
    quotes: list[MarketQuote],
    cutoff: datetime,
    end_clock: time,
    history_sessions: int,
    tolerance_minutes: int,
) -> tuple[Decimal, Decimal, list[str]] | None:
    by_session: dict[date, MarketQuote] = {}
    cutoff_local = cutoff.astimezone(NEW_YORK)
    end_minutes = end_clock.hour * 60 + end_clock.minute
    for quote in quotes:
        if (
            quote.symbol != symbol
            or quote.available_at > cutoff
            or quote.event_time > cutoff
            or quote.bid_price <= 0
            or quote.ask_price <= quote.bid_price
        ):
            continue
        local = quote.event_time.astimezone(NEW_YORK)
        quote_minutes = local.hour * 60 + local.minute
        if quote_minutes > end_minutes or end_minutes - quote_minutes > tolerance_minutes:
            continue
        current = by_session.get(local.date())
        if current is None or (quote.event_time, quote.available_at, quote.quote_id) > (
            current.event_time,
            current.available_at,
            current.quote_id,
        ):
            by_session[local.date()] = quote

    current_quote = by_session.get(cutoff_local.date())
    prior_dates = sorted(item for item in by_session if item < cutoff_local.date())
    if current_quote is None or len(prior_dates) < history_sessions:
        return None
    selected_dates = prior_dates[-history_sessions:]
    history = [_spread_bps(by_session[item]) for item in selected_dates]
    source_ids = [
        current_quote.quote_id,
        *(by_session[item].quote_id for item in selected_dates),
    ]
    return _spread_bps(current_quote), median(history), source_ids


def _spread_bps(quote: MarketQuote) -> Decimal:
    midpoint = (quote.bid_price + quote.ask_price) / Decimal("2")
    return (quote.ask_price - quote.bid_price) / midpoint * Decimal("10000")


def _clock_minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def _set_intersection[T](sets: Iterable[set[T]]) -> set[T]:
    iterator = iter(sets)
    try:
        result = set(next(iterator))
    except StopIteration:
        return set()
    for values in iterator:
        result.intersection_update(values)
    return result
