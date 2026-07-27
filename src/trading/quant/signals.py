from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from trading.domain.hashing import canonical_hash
from trading.quant.config import Q1MathConfig
from trading.quant.covariance import CovarianceEstimate, Q1MathError, ewma_covariance


@dataclass(frozen=True, slots=True)
class AdjustedCloseObservation:
    bar_id: str
    symbol: str
    session_id: str
    session_close_at: datetime
    adjusted_close: Decimal
    available_at: datetime


@dataclass(frozen=True, slots=True)
class AssetTrend:
    symbol: str
    log_returns_by_horizon: tuple[tuple[int, Decimal], ...]
    z_scores_by_horizon: tuple[tuple[int, Decimal], ...]
    trend_score: Decimal
    annualized_sigma: Decimal

    def return_for(self, horizon_sessions: int) -> Decimal:
        return _lookup(self.log_returns_by_horizon, horizon_sessions)

    def z_score_for(self, horizon_sessions: int) -> Decimal:
        return _lookup(self.z_scores_by_horizon, horizon_sessions)


@dataclass(frozen=True, slots=True)
class Q1Signal:
    calendar_session_id: str
    latest_completed_session_id: str
    scheduled_at: datetime
    signal_data_cutoff: datetime
    completed_session_ids: tuple[str, ...]
    source_bar_ids: tuple[str, ...]
    covariance: CovarianceEstimate
    trends: tuple[AssetTrend, ...]
    relative_strength: Decimal
    spread_return: Decimal
    spread_variance: Decimal
    market_gate: Decimal
    raw_scores: tuple[tuple[str, Decimal], ...]
    confidence: Decimal
    config_manifest_hash: str
    signal_model_version: str
    signal_hash: str

    def trend_for(self, symbol: str) -> AssetTrend:
        try:
            return next(item for item in self.trends if item.symbol == symbol)
        except StopIteration as exc:
            raise Q1MathError(f"missing trend for {symbol}") from exc

    def score_for(self, symbol: str) -> Decimal:
        return _lookup(self.raw_scores, symbol)


def compute_q1_signal(
    observations: Sequence[AdjustedCloseObservation],
    *,
    completed_session_ids: Sequence[str],
    calendar_session_id: str,
    expected_latest_completed_session_id: str,
    current_session_open_at: datetime,
    scheduled_at: datetime,
    signal_data_cutoff: datetime,
    config: Q1MathConfig,
    config_manifest_hash: str,
) -> Q1Signal:
    """Compute the exact Q1 point-in-time signal from completed adjusted closes."""

    _require_aware(current_session_open_at, "current_session_open_at")
    _require_aware(scheduled_at, "scheduled_at")
    _require_aware(signal_data_cutoff, "signal_data_cutoff")
    if signal_data_cutoff > scheduled_at:
        raise Q1MathError("signal_data_cutoff must not exceed scheduled_at")
    sessions = tuple(completed_session_ids)
    if len(sessions) < config.signal.minimum_completed_sessions:
        raise Q1MathError("insufficient completed sessions")
    if len(set(sessions)) != len(sessions) or any(not item for item in sessions):
        raise Q1MathError("completed session IDs must be unique and non-empty")
    if not calendar_session_id:
        raise Q1MathError("calendar_session_id is required")
    if (
        not expected_latest_completed_session_id
        or sessions[-1] != expected_latest_completed_session_id
    ):
        raise Q1MathError("completed daily data is stale for the versioned market calendar")
    if not config_manifest_hash:
        raise Q1MathError("config_manifest_hash is required")

    selected, source_ids = _select_point_in_time_prices(
        observations,
        symbols=config.risky_symbols,
        completed_session_ids=sessions,
        current_session_open_at=current_session_open_at,
        cutoff=signal_data_cutoff,
    )
    with localcontext() as context:
        context.prec = config.covariance.decimal_precision
        returns = {
            symbol: tuple(
                _quantize_return(
                    (prices[index] / prices[index - 1]).ln(),
                    config,
                )
                for index in range(1, len(prices))
            )
            for symbol, prices in selected.items()
        }
        covariance = ewma_covariance(returns, parameters=config.covariance)
        trends = tuple(
            _asset_trend(symbol, selected[symbol], covariance, config)
            for symbol in config.risky_symbols
        )
        qqq = next(item for item in trends if item.symbol == "QQQ")
        soxx = next(item for item in trends if item.symbol == "SOXX")
        relative_horizon = config.signal.relative_strength_horizon_sessions
        spread_return = (
            soxx.return_for(relative_horizon) - qqq.return_for(relative_horizon)
        )
        spread_variance = (
            covariance.variance("QQQ")
            + covariance.variance("SOXX")
            - Decimal("2") * covariance.value("QQQ", "SOXX")
        )
        if spread_variance <= 0:
            raise Q1MathError("relative-strength spread variance must be positive")
        spread_denominator = (
            Decimal(relative_horizon)
            / Decimal(config.covariance.annualization_sessions)
            * spread_variance
        ).sqrt()
        relative_strength = _clip(
            spread_return / spread_denominator,
            -config.signal.z_score_clip,
            config.signal.z_score_clip,
        )
        market_gate = _clip(
            (qqq.trend_score + config.signal.market_gate_offset)
            / config.signal.market_gate_width,
            config.signal.market_gate_min,
            config.signal.market_gate_max,
        )
        qqq_score = max(Decimal("0"), qqq.trend_score)
        soxx_score = (
            max(
                Decimal("0"),
                soxx.trend_score
                + config.signal.relative_strength_coefficient * relative_strength,
            )
            * market_gate
        )
        raw_scores = (("QQQ", qqq_score), ("SOXX", soxx_score))
        confidence = _clip(
            (qqq_score + soxx_score) / config.signal.confidence_denominator,
            config.signal.confidence_min,
            config.signal.confidence_max,
        )
        hash_payload = {
            "algorithm_version": config.algorithm_version,
            "signal_model_version": config.signal_model_version,
            "config_manifest_hash": config_manifest_hash,
            "calendar_session_id": calendar_session_id,
            "latest_completed_session_id": expected_latest_completed_session_id,
            "scheduled_at": scheduled_at,
            "signal_data_cutoff": signal_data_cutoff,
            "completed_session_ids": sessions,
            "source_bar_ids": source_ids,
            "selected_adjusted_closes": {
                symbol: list(selected[symbol])
                for symbol in config.risky_symbols
            },
            "covariance": covariance.as_mapping(),
            "trends": [
                {
                    "symbol": item.symbol,
                    "returns": item.log_returns_by_horizon,
                    "z_scores": item.z_scores_by_horizon,
                    "trend": item.trend_score,
                    "annualized_sigma": item.annualized_sigma,
                }
                for item in trends
            ],
            "spread_return": spread_return,
            "spread_variance": spread_variance,
            "relative_strength": relative_strength,
            "market_gate": market_gate,
            "raw_scores": raw_scores,
            "confidence": confidence,
        }
        return Q1Signal(
            calendar_session_id=calendar_session_id,
            latest_completed_session_id=expected_latest_completed_session_id,
            scheduled_at=scheduled_at,
            signal_data_cutoff=signal_data_cutoff,
            completed_session_ids=sessions,
            source_bar_ids=source_ids,
            covariance=covariance,
            trends=trends,
            relative_strength=+relative_strength,
            spread_return=+spread_return,
            spread_variance=+spread_variance,
            market_gate=+market_gate,
            raw_scores=tuple((symbol, +score) for symbol, score in raw_scores),
            confidence=+confidence,
            config_manifest_hash=config_manifest_hash,
            signal_model_version=config.signal_model_version,
            signal_hash=canonical_hash(hash_payload),
        )


def _select_point_in_time_prices(
    observations: Sequence[AdjustedCloseObservation],
    *,
    symbols: tuple[str, str],
    completed_session_ids: tuple[str, ...],
    current_session_open_at: datetime,
    cutoff: datetime,
) -> tuple[dict[str, tuple[Decimal, ...]], tuple[str, ...]]:
    expected = set(completed_session_ids)
    grouped: dict[tuple[str, str], list[AdjustedCloseObservation]] = defaultdict(list)
    for observation in observations:
        normalized_symbol = observation.symbol.strip().upper()
        if normalized_symbol not in symbols or observation.session_id not in expected:
            continue
        _require_aware(observation.available_at, "bar.available_at")
        _require_aware(observation.session_close_at, "bar.session_close_at")
        if observation.available_at > cutoff:
            continue
        if observation.session_close_at >= current_session_open_at:
            raise Q1MathError("current-session or future daily bar is not completed")
        if observation.available_at < observation.session_close_at:
            raise Q1MathError("adjusted close cannot be available before its session close")
        if observation.adjusted_close <= 0 or not observation.adjusted_close.is_finite():
            raise Q1MathError("adjusted closes must be positive and finite")
        if not observation.bar_id:
            raise Q1MathError("source bar ID is required")
        grouped[(normalized_symbol, observation.session_id)].append(observation)

    prices: dict[str, tuple[Decimal, ...]] = {}
    source_ids: list[str] = []
    previous_close_at: datetime | None = None
    for session_id in completed_session_ids:
        session_close_values: set[datetime] = set()
        for symbol in symbols:
            candidates = grouped.get((symbol, session_id), [])
            if not candidates:
                raise Q1MathError(f"missing point-in-time adjusted close for {symbol}/{session_id}")
            distinct_prices = {item.adjusted_close for item in candidates}
            distinct_closes = {item.session_close_at for item in candidates}
            if len(distinct_prices) != 1 or len(distinct_closes) != 1:
                raise Q1MathError(
                    f"inconsistent duplicate adjusted close for {symbol}/{session_id}"
                )
            session_close_values.update(distinct_closes)
        if len(session_close_values) != 1:
            raise Q1MathError(f"aligned symbols disagree on session close for {session_id}")
        close_at = next(iter(session_close_values))
        if previous_close_at is not None and close_at <= previous_close_at:
            raise Q1MathError("completed sessions must be strictly chronological")
        previous_close_at = close_at

    for symbol in symbols:
        symbol_prices: list[Decimal] = []
        for session_id in completed_session_ids:
            candidates = grouped[(symbol, session_id)]
            canonical = min(candidates, key=lambda item: (item.bar_id, item.available_at))
            symbol_prices.append(canonical.adjusted_close)
            source_ids.extend(sorted(item.bar_id for item in candidates))
        prices[symbol] = tuple(symbol_prices)
    return prices, tuple(source_ids)


def _asset_trend(
    symbol: str,
    prices: tuple[Decimal, ...],
    covariance: CovarianceEstimate,
    config: Q1MathConfig,
) -> AssetTrend:
    variance = covariance.variance(symbol)
    annualized_sigma = variance.sqrt()
    log_returns: list[tuple[int, Decimal]] = []
    z_scores: list[tuple[int, Decimal]] = []
    for horizon in config.signal.horizons_sessions:
        horizon_return = _quantize_return(
            (prices[-1] / prices[-1 - horizon]).ln(),
            config,
        )
        denominator = (
            Decimal(horizon)
            / Decimal(config.covariance.annualization_sessions)
            * variance
        ).sqrt()
        z_score = _clip(
            horizon_return / denominator,
            -config.signal.z_score_clip,
            config.signal.z_score_clip,
        )
        log_returns.append((horizon, +horizon_return))
        z_scores.append((horizon, +z_score))
    ordered = sorted(value for _, value in z_scores)
    trend_score = ordered[len(ordered) // 2]
    return AssetTrend(
        symbol=symbol,
        log_returns_by_horizon=tuple(log_returns),
        z_scores_by_horizon=tuple(z_scores),
        trend_score=+trend_score,
        annualized_sigma=+annualized_sigma,
    )


def _clip(value: Decimal, lower: Decimal, upper: Decimal) -> Decimal:
    return max(lower, min(upper, value))


def _quantize_return(value: Decimal, config: Q1MathConfig) -> Decimal:
    if config.signal.numeric_rounding != "HALF_EVEN":
        raise Q1MathError("unsupported numeric rounding mode")
    return value.quantize(config.signal.return_quantum, rounding=ROUND_HALF_EVEN)


def _lookup(items: tuple[tuple[object, Decimal], ...], key: object) -> Decimal:
    try:
        return next(value for item_key, value in items if item_key == key)
    except StopIteration as exc:
        raise Q1MathError(f"missing diagnostic value for {key}") from exc


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise Q1MathError(f"{name} must be timezone-aware")
