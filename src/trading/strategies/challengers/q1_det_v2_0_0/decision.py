from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

STRATEGY_ID = "Q1-DET"
STRATEGY_VERSION = "2.0.0"
PARENT_VERSION = "1.0.0"
HYPOTHESIS_ID = "hypothesis-q1-residual-defensive-sleeve-v1"

_SLEEVE_ORDER = ("GLD", "SGOV", "TLT")
_SLEEVE_SYMBOLS = frozenset(_SLEEVE_ORDER)
_DIVERSIFIERS = ("GLD", "TLT")
_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.-]{0,14}$")
_MISSING = object()


@dataclass(frozen=True, slots=True)
class _Instrument:
    symbol: str
    current_weight: float
    features: dict[str, float]
    feature_integrity: bool


@dataclass(frozen=True, slots=True)
class _Parameters:
    review_interval_sessions: int = 21
    review_anchor_session_ordinal: int = 0
    short_return_sessions: int = 63
    long_return_sessions: int = 126
    moving_average_sessions: int = 200
    downside_beta_sessions: int = 126
    minimum_downside_observations: int = 20
    sleeve_cap: float = 0.35
    diversifier_cap: float = 0.20
    no_trade_band: float = 0.02
    entry_excess_trend: float = 0.0
    exit_excess_trend: float = -0.02
    entry_moving_average_gap: float = 0.0
    exit_moving_average_gap: float = -0.02
    gld_entry_downside_beta: float = 0.25
    gld_exit_downside_beta: float = 0.35
    tlt_entry_downside_beta: float = 0.0
    tlt_exit_downside_beta: float = 0.10


@dataclass(frozen=True, slots=True)
class _Signal:
    score: float
    volatility: float | None
    valid: bool
    eligible: bool


def decide(request: dict[str, Any]) -> dict[str, Any]:
    """Return one deterministic, constrained long-only target per input symbol."""

    context = _parse_request(request)
    instruments: dict[str, _Instrument] = context["instruments"]
    caps: dict[str, float] = context["caps"]
    gross_limit: float = context["gross_limit"]
    tolerance: float = context["tolerance"]

    parameters, parameters_valid = _load_parameters(
        context["strategy_parameters"]
    )
    strategy_binding_valid = (
        context["strategy_id"] == STRATEGY_ID
        and context["strategy_version"] == STRATEGY_VERSION
    )

    weights = {symbol: 0.0 for symbol in sorted(instruments)}
    parent_targets_preserved = True
    for symbol, instrument in sorted(instruments.items()):
        if symbol in _SLEEVE_SYMBOLS:
            continue
        target, came_from_parent = _parent_target(instrument)
        weights[symbol] = min(target, caps[symbol])
        if symbol in {"QQQ", "SOXX"} and not came_from_parent:
            parent_targets_preserved = False

    base_symbols = tuple(
        symbol for symbol in weights if symbol not in _SLEEVE_SYMBOLS
    )
    base_gross = sum(weights[symbol] for symbol in base_symbols)
    if base_gross > gross_limit:
        weights = _scale_selected(weights, base_symbols, gross_limit)
        base_gross = sum(weights[symbol] for symbol in base_symbols)
        parent_targets_preserved = False

    sleeve_budget = max(
        0.0,
        min(
            parameters.sleeve_cap,
            1.0 - base_gross,
            gross_limit - base_gross,
        ),
    )
    review_due, schedule_source = _review_due(instruments, parameters)
    review_due = review_due and parameters_valid and strategy_binding_valid
    signals, reserve_valid = _signals(instruments, parameters, tolerance)
    no_trade_band_applied: list[str] = []

    if review_due:
        sleeve_targets = _reviewed_sleeve_targets(
            instruments=instruments,
            caps=caps,
            budget=sleeve_budget,
            parameters=parameters,
            signals=signals,
            reserve_valid=reserve_valid,
        )
        for symbol in _SLEEVE_ORDER:
            if symbol not in instruments:
                continue
            desired = sleeve_targets.get(symbol, 0.0)
            current = min(instruments[symbol].current_weight, caps[symbol])
            signal_valid = (
                reserve_valid if symbol == "SGOV" else signals[symbol].valid
            )
            if (
                signal_valid
                and abs(desired - current) < parameters.no_trade_band
            ):
                sleeve_targets[symbol] = current
                no_trade_band_applied.append(symbol)
        sleeve_targets = _fit_sleeve(
            sleeve_targets,
            caps=caps,
            budget=sleeve_budget,
        )
    else:
        sleeve_targets = _fit_sleeve(
            {
                symbol: min(instruments[symbol].current_weight, caps[symbol])
                for symbol in _SLEEVE_ORDER
                if symbol in instruments
            },
            caps=caps,
            budget=sleeve_budget,
        )

    weights.update(sleeve_targets)
    weights = _enforce_host_limits(
        weights,
        caps=caps,
        gross_limit=gross_limit,
    )

    scores: dict[str, float] = {}
    for symbol, instrument in instruments.items():
        if symbol in signals:
            scores[symbol] = signals[symbol].score
        elif symbol in {"QQQ", "SOXX"}:
            scores[symbol] = _feature(
                instrument,
                ("parent_score", "q1_det_parent_score"),
                default=weights[symbol],
            )
        else:
            scores[symbol] = 0.0

    targets = [
        {
            "symbol": symbol,
            "score": _canonical_float(scores[symbol]),
            "target_weight": weights[symbol],
        }
        for symbol in sorted(instruments)
    ]
    eligible = sorted(
        symbol for symbol, signal in signals.items() if signal.eligible
    )
    diagnostics: dict[str, Any] = {
        "strategy_id": STRATEGY_ID,
        "strategy_version": STRATEGY_VERSION,
        "parent_version": PARENT_VERSION,
        "hypothesis_id": HYPOTHESIS_ID,
        "strategy_binding_valid": strategy_binding_valid,
        "parameters_valid": parameters_valid,
        "input_feature_integrity": all(
            instrument.feature_integrity for instrument in instruments.values()
        ),
        "review_due": review_due,
        "review_schedule_source": schedule_source,
        "review_interval_sessions": parameters.review_interval_sessions,
        "implementation_delay_sessions": 1,
        "parent_targets_preserved": parent_targets_preserved,
        "eligible_diversifiers": eligible,
        "reserve_available": reserve_valid,
        "sleeve_budget": _canonical_float(sleeve_budget),
        "sleeve_gross": _canonical_float(
            sum(weights.get(symbol, 0.0) for symbol in _SLEEVE_ORDER)
        ),
        "no_trade_band_applied": sorted(no_trade_band_applied),
        "scope": "long_only_targets",
    }
    payload: dict[str, Any] = {
        "schema_version": "candidate_decision_response_v1",
        "request_id": context["request_id"],
        "request_hash": context["request_hash"],
        "challenger_id": context["challenger_id"],
        "candidate_artifact_hash": context["candidate_artifact_hash"],
        "targets": targets,
        "diagnostics": diagnostics,
    }
    return {**payload, "output_hash": _canonical_hash(payload)}


def _parse_request(request: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise ValueError("candidate request must be a raw JSON object")
    if request.get("schema_version") != "candidate_decision_request_v1":
        raise ValueError("unsupported candidate request schema")

    request_id = _required_string(request, "request_id")
    request_hash = _required_hash(request, "request_hash")
    challenger_id = _required_string(request, "challenger_id")
    candidate_artifact_hash = _required_hash(request, "candidate_artifact_hash")
    strategy_id = _required_string(request, "strategy_id")
    strategy_version = _required_string(request, "strategy_version")
    decision_time = _timestamp(request.get("decision_time"))
    signal_data_cutoff = _timestamp(request.get("signal_data_cutoff"))
    if signal_data_cutoff > decision_time:
        raise ValueError("signal_data_cutoff cannot exceed decision_time")

    raw_constraints_value = request.get("constraints")
    if not isinstance(raw_constraints_value, dict):
        raise ValueError("candidate constraints must be an object")
    raw_constraints = cast(dict[str, Any], raw_constraints_value)
    if (
        raw_constraints.get("long_only") is not True
        or raw_constraints.get("leverage_permitted") is not False
        or raw_constraints.get("new_symbols_permitted") is not False
    ):
        raise ValueError("unsafe candidate constraints")
    maximum_gross = _required_number(
        raw_constraints, "maximum_gross_weight", lower=0.0, upper=1.0
    )
    minimum_cash = _required_number(
        raw_constraints,
        "minimum_cash_weight",
        lower=0.0,
        upper=1.0,
        lower_inclusive=True,
    )
    if minimum_cash >= 1.0:
        raise ValueError("minimum_cash_weight must be below one")
    tolerance = _required_number(
        raw_constraints, "numeric_tolerance", lower=0.0
    )
    raw_caps_value = raw_constraints.get("maximum_weight_by_symbol")
    if not isinstance(raw_caps_value, dict) or not raw_caps_value:
        raise ValueError("candidate symbol caps must be a non-empty object")
    raw_caps = cast(dict[str, Any], raw_caps_value)
    caps: dict[str, float] = {}
    for symbol, value in raw_caps.items():
        if not isinstance(symbol, str) or _SYMBOL_PATTERN.fullmatch(symbol) is None:
            raise ValueError("invalid symbol cap")
        number = _number(value)
        if number is None or not 0.0 <= number <= 1.0:
            raise ValueError("invalid symbol cap")
        caps[symbol] = number

    raw_instruments_value = request.get("instruments")
    if not isinstance(raw_instruments_value, list) or not raw_instruments_value:
        raise ValueError("candidate instruments must be a non-empty array")
    raw_instruments = cast(list[Any], raw_instruments_value)
    instruments: dict[str, _Instrument] = {}
    for raw_instrument in raw_instruments:
        instrument = _parse_instrument(
            raw_instrument,
            decision_time=decision_time,
            signal_data_cutoff=signal_data_cutoff,
        )
        if instrument.symbol in instruments:
            raise ValueError("duplicate candidate instrument")
        instruments[instrument.symbol] = instrument
    if set(instruments) != set(caps):
        raise ValueError("candidate universe differs from host symbol caps")

    raw_parameters_value = request.get("strategy_parameters")
    if not isinstance(raw_parameters_value, dict):
        raise ValueError("strategy_parameters must be an object")
    raw_parameters = cast(dict[str, Any], raw_parameters_value)
    gross_limit = min(maximum_gross, 1.0 - minimum_cash)
    if gross_limit <= 0.0:
        raise ValueError("host gross boundary must be positive")
    return {
        "request_id": request_id,
        "request_hash": request_hash,
        "challenger_id": challenger_id,
        "candidate_artifact_hash": candidate_artifact_hash,
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "strategy_parameters": raw_parameters,
        "instruments": instruments,
        "caps": caps,
        "gross_limit": gross_limit,
        "tolerance": tolerance,
    }


def _parse_instrument(
    raw: object,
    *,
    decision_time: datetime,
    signal_data_cutoff: datetime,
) -> _Instrument:
    if not isinstance(raw, dict):
        raise ValueError("candidate instrument must be an object")
    data = cast(dict[str, Any], raw)
    symbol = _required_string(data, "symbol")
    if _SYMBOL_PATTERN.fullmatch(symbol) is None:
        raise ValueError("invalid candidate symbol")
    current_weight = _required_number(
        data,
        "current_weight",
        lower=0.0,
        upper=1.0,
        lower_inclusive=True,
    )

    integrity = True
    try:
        membership_available_at = _timestamp(data.get("membership_available_at"))
        membership_valid_from = _timestamp(data.get("membership_valid_from"))
        valid_until_raw = data.get("membership_valid_until")
        membership_valid_until = (
            None if valid_until_raw is None else _timestamp(valid_until_raw)
        )
        is_non_survivor = data.get("instrument_is_non_survivor")
        if not isinstance(is_non_survivor, bool):
            raise ValueError("instrument_is_non_survivor must be boolean")
        if (
            membership_available_at > signal_data_cutoff
            or membership_valid_from > decision_time
            or (
                membership_valid_until is not None
                and membership_valid_until < decision_time
            )
            or (is_non_survivor and membership_valid_until is None)
        ):
            integrity = False
    except (TypeError, ValueError):
        integrity = False

    raw_features_value = data.get("features")
    features: dict[str, float] = {}
    if not isinstance(raw_features_value, list) or not raw_features_value:
        integrity = False
    else:
        raw_features = cast(list[Any], raw_features_value)
        for raw_feature in raw_features:
            parsed = _parse_feature(
                raw_feature,
                signal_data_cutoff=signal_data_cutoff,
            )
            if parsed is None:
                integrity = False
                continue
            name, value = parsed
            if name in features:
                integrity = False
            features[name] = value
    if not integrity:
        features = {}
    return _Instrument(
        symbol=symbol,
        current_weight=current_weight,
        features=features,
        feature_integrity=integrity,
    )


def _parse_feature(
    raw: object,
    *,
    signal_data_cutoff: datetime,
) -> tuple[str, float] | None:
    if not isinstance(raw, dict):
        return None
    data = cast(dict[str, Any], raw)
    name = data.get("name")
    value = _number(data.get("value"))
    source_revision = data.get("source_revision")
    source_hash = data.get("source_hash")
    if (
        not isinstance(name, str)
        or not name
        or value is None
        or isinstance(source_revision, bool)
        or not isinstance(source_revision, int)
        or source_revision < 0
        or data.get("revision_was_known_at_cutoff") is not True
        or not isinstance(source_hash, str)
        or _SHA256_PATTERN.fullmatch(source_hash) is None
    ):
        return None
    try:
        source_event_time = _timestamp(data.get("source_event_time"))
        available_at = _timestamp(data.get("available_at"))
        revision_available_at = _timestamp(data.get("revision_available_at"))
    except (TypeError, ValueError):
        return None
    if (
        source_event_time > available_at
        or source_event_time > signal_data_cutoff
        or available_at > signal_data_cutoff
        or revision_available_at > signal_data_cutoff
    ):
        return None
    return name, value


def _load_parameters(raw: dict[str, Any]) -> tuple[_Parameters, bool]:
    source = dict(raw)
    for nested_name in ("q1_det_v2_0_0", "residual_defensive_sleeve"):
        nested = raw.get(nested_name)
        if isinstance(nested, dict):
            source.update(cast(dict[str, Any], nested))

    valid = True

    def integer(
        names: tuple[str, ...],
        default: int,
        *,
        minimum: int,
        maximum: int,
    ) -> int:
        nonlocal valid
        value = _first_present(source, names)
        if value is _MISSING:
            return default
        number = _number(value)
        if (
            number is None
            or not number.is_integer()
            or not minimum <= number <= maximum
        ):
            valid = False
            return default
        return int(number)

    def floating(
        names: tuple[str, ...],
        default: float,
        *,
        minimum: float,
        maximum: float,
    ) -> float:
        nonlocal valid
        value = _first_present(source, names)
        if value is _MISSING:
            return default
        number = _number(value)
        if number is None or not minimum <= number <= maximum:
            valid = False
            return default
        return number

    parameters = _Parameters(
        review_interval_sessions=integer(
            ("review_interval_sessions",), 21, minimum=1, maximum=252
        ),
        review_anchor_session_ordinal=integer(
            ("review_anchor_session_ordinal", "review_anchor_session"),
            0,
            minimum=0,
            maximum=10_000_000,
        ),
        short_return_sessions=integer(
            ("short_return_sessions", "return_lookback_short"),
            63,
            minimum=2,
            maximum=504,
        ),
        long_return_sessions=integer(
            ("long_return_sessions", "return_lookback_long"),
            126,
            minimum=2,
            maximum=756,
        ),
        moving_average_sessions=integer(
            ("moving_average_sessions", "moving_average_lookback"),
            200,
            minimum=2,
            maximum=756,
        ),
        downside_beta_sessions=integer(
            ("downside_beta_sessions", "downside_beta_lookback"),
            126,
            minimum=2,
            maximum=756,
        ),
        minimum_downside_observations=integer(
            ("minimum_downside_observations",),
            20,
            minimum=1,
            maximum=756,
        ),
        sleeve_cap=floating(
            ("sleeve_cap",), 0.35, minimum=0.0, maximum=1.0
        ),
        diversifier_cap=floating(
            ("diversifier_cap", "per_diversifier_cap"),
            0.20,
            minimum=0.0,
            maximum=1.0,
        ),
        no_trade_band=floating(
            ("no_trade_band",), 0.02, minimum=0.0, maximum=1.0
        ),
        entry_excess_trend=floating(
            ("entry_excess_trend",), 0.0, minimum=-10.0, maximum=10.0
        ),
        exit_excess_trend=floating(
            ("exit_excess_trend",), -0.02, minimum=-10.0, maximum=10.0
        ),
        entry_moving_average_gap=floating(
            ("entry_moving_average_gap",),
            0.0,
            minimum=-10.0,
            maximum=10.0,
        ),
        exit_moving_average_gap=floating(
            ("exit_moving_average_gap",),
            -0.02,
            minimum=-10.0,
            maximum=10.0,
        ),
        gld_entry_downside_beta=floating(
            ("gld_entry_downside_beta",),
            0.25,
            minimum=-10.0,
            maximum=10.0,
        ),
        gld_exit_downside_beta=floating(
            ("gld_exit_downside_beta",),
            0.35,
            minimum=-10.0,
            maximum=10.0,
        ),
        tlt_entry_downside_beta=floating(
            ("tlt_entry_downside_beta",),
            0.0,
            minimum=-10.0,
            maximum=10.0,
        ),
        tlt_exit_downside_beta=floating(
            ("tlt_exit_downside_beta",),
            0.10,
            minimum=-10.0,
            maximum=10.0,
        ),
    )
    if (
        parameters.short_return_sessions >= parameters.long_return_sessions
        or parameters.exit_excess_trend > parameters.entry_excess_trend
        or (
            parameters.exit_moving_average_gap
            > parameters.entry_moving_average_gap
        )
        or (
            parameters.gld_exit_downside_beta
            < parameters.gld_entry_downside_beta
        )
        or (
            parameters.tlt_exit_downside_beta
            < parameters.tlt_entry_downside_beta
        )
    ):
        valid = False
    return parameters, valid


def _review_due(
    instruments: dict[str, _Instrument],
    parameters: _Parameters,
) -> tuple[bool, str]:
    priority = ("QQQ", "SOXX", "SGOV", "GLD", "TLT")
    ordered = [
        instruments[symbol] for symbol in priority if symbol in instruments
    ]
    ordered.extend(
        instrument
        for symbol, instrument in sorted(instruments.items())
        if symbol not in priority
    )
    for instrument in ordered:
        direct = _feature_optional(
            instrument, ("review_due", "sleeve_review_due")
        )
        if direct is not None and direct in {0.0, 1.0}:
            return direct == 1.0, f"{instrument.symbol}:review_due"
        since_review = _feature_optional(
            instrument,
            (
                "completed_sessions_since_review",
                "completed_sessions_since_last_review",
            ),
        )
        if since_review is not None and _is_nonnegative_integer(since_review):
            return (
                int(since_review) >= parameters.review_interval_sessions,
                f"{instrument.symbol}:sessions_since_review",
            )
        ordinal = _feature_optional(
            instrument,
            ("completed_session_ordinal", "market_session_ordinal"),
        )
        if ordinal is not None and _is_nonnegative_integer(ordinal):
            offset = int(ordinal) - parameters.review_anchor_session_ordinal
            return (
                offset >= 0
                and offset % parameters.review_interval_sessions == 0,
                f"{instrument.symbol}:session_ordinal",
            )
    return False, "missing"


def _signals(
    instruments: dict[str, _Instrument],
    parameters: _Parameters,
    tolerance: float,
) -> tuple[dict[str, _Signal], bool]:
    reserve = instruments.get("SGOV")
    reserve_short = (
        None
        if reserve is None
        else _return_feature(reserve, parameters.short_return_sessions)
    )
    reserve_long = (
        None
        if reserve is None
        else _return_feature(reserve, parameters.long_return_sessions)
    )
    reserve_valid = (
        reserve is not None
        and reserve.feature_integrity
        and reserve_short is not None
        and reserve_long is not None
    )

    signals: dict[str, _Signal] = {}
    for symbol in _DIVERSIFIERS:
        instrument = instruments.get(symbol)
        if instrument is None:
            continue
        short_return = _return_feature(
            instrument, parameters.short_return_sessions
        )
        long_return = _return_feature(
            instrument, parameters.long_return_sessions
        )
        volatility = _volatility_feature(
            instrument, parameters.short_return_sessions
        )
        moving_average_gap = _moving_average_gap(
            instrument, parameters.moving_average_sessions
        )
        downside_beta = _downside_beta_feature(
            instrument, parameters.downside_beta_sessions
        )
        downside_observations = _downside_observations_feature(
            instrument, parameters.downside_beta_sessions
        )
        score_valid = (
            reserve_valid
            and short_return is not None
            and long_return is not None
        )
        score = (
            0.5 * (short_return - reserve_short)
            + 0.5 * (long_return - reserve_long)
            if score_valid
            and reserve_short is not None
            and reserve_long is not None
            and short_return is not None
            and long_return is not None
            else 0.0
        )
        if not math.isfinite(score):
            score = 0.0
            score_valid = False
        valid = (
            instrument.feature_integrity
            and score_valid
            and volatility is not None
            and volatility > 0.0
            and moving_average_gap is not None
            and downside_beta is not None
            and downside_observations is not None
            and _is_nonnegative_integer(downside_observations)
            and downside_observations
            >= parameters.minimum_downside_observations
        )
        held = instrument.current_weight > tolerance
        if symbol == "GLD":
            beta_threshold = (
                parameters.gld_exit_downside_beta
                if held
                else parameters.gld_entry_downside_beta
            )
        else:
            beta_threshold = (
                parameters.tlt_exit_downside_beta
                if held
                else parameters.tlt_entry_downside_beta
            )
        trend_threshold = (
            parameters.exit_excess_trend
            if held
            else parameters.entry_excess_trend
        )
        average_threshold = (
            parameters.exit_moving_average_gap
            if held
            else parameters.entry_moving_average_gap
        )
        eligible = bool(
            valid
            and moving_average_gap is not None
            and downside_beta is not None
            and (
                score >= trend_threshold
                if held
                else score > trend_threshold
            )
            and (
                moving_average_gap >= average_threshold
                if held
                else moving_average_gap > average_threshold
            )
            and downside_beta < beta_threshold
        )
        signals[symbol] = _Signal(
            score=score,
            volatility=volatility,
            valid=valid,
            eligible=eligible,
        )
    return signals, reserve_valid


def _reviewed_sleeve_targets(
    *,
    instruments: dict[str, _Instrument],
    caps: dict[str, float],
    budget: float,
    parameters: _Parameters,
    signals: dict[str, _Signal],
    reserve_valid: bool,
) -> dict[str, float]:
    targets = {
        symbol: 0.0 for symbol in _SLEEVE_ORDER if symbol in instruments
    }
    eligible_volatility: dict[str, float] = {}
    for symbol in _DIVERSIFIERS:
        signal = signals.get(symbol)
        if signal is not None and signal.eligible and signal.volatility is not None:
            eligible_volatility[symbol] = signal.volatility
    minimum_volatility = min(eligible_volatility.values(), default=None)
    inverse_volatility = (
        {
            symbol: minimum_volatility / volatility
            for symbol, volatility in eligible_volatility.items()
        }
        if minimum_volatility is not None
        else {}
    )
    inverse_total = sum(inverse_volatility.values())
    if inverse_total > 0.0:
        for symbol in eligible_volatility:
            uncapped = budget * inverse_volatility[symbol] / inverse_total
            targets[symbol] = min(
                uncapped,
                parameters.diversifier_cap,
                caps[symbol],
            )
    unused = max(0.0, budget - sum(targets.values()))
    if reserve_valid and "SGOV" in targets:
        targets["SGOV"] = min(unused, caps["SGOV"])
    return targets


def _fit_sleeve(
    targets: dict[str, float],
    *,
    caps: dict[str, float],
    budget: float,
) -> dict[str, float]:
    fitted = {
        symbol: min(max(0.0, target), caps[symbol])
        for symbol, target in targets.items()
    }
    excess = sum(fitted.values()) - budget
    if excess <= 0.0:
        return fitted
    if "SGOV" in fitted:
        reduction = min(excess, fitted["SGOV"])
        fitted["SGOV"] -= reduction
        excess -= reduction
    if excess > 0.0:
        diversifier_gross = sum(
            fitted.get(symbol, 0.0) for symbol in _DIVERSIFIERS
        )
        if diversifier_gross > 0.0:
            scale = max(0.0, (diversifier_gross - excess) / diversifier_gross)
            for symbol in _DIVERSIFIERS:
                if symbol in fitted:
                    fitted[symbol] *= scale
    return fitted


def _enforce_host_limits(
    weights: dict[str, float],
    *,
    caps: dict[str, float],
    gross_limit: float,
) -> dict[str, float]:
    fitted = {
        symbol: min(max(0.0, weights.get(symbol, 0.0)), caps[symbol])
        for symbol in sorted(caps)
    }
    excess = sum(fitted.values()) - gross_limit
    if excess > 0.0:
        reduction_order = [
            symbol for symbol in ("SGOV", "TLT", "GLD") if symbol in fitted
        ]
        reduction_order.extend(
            symbol
            for symbol in reversed(sorted(fitted))
            if symbol not in reduction_order
        )
        for symbol in reduction_order:
            reduction = min(excess, fitted[symbol])
            fitted[symbol] -= reduction
            excess = sum(fitted.values()) - gross_limit
            if excess <= 0.0:
                break
    return fitted


def _scale_selected(
    weights: dict[str, float],
    symbols: tuple[str, ...],
    limit: float,
) -> dict[str, float]:
    total = sum(weights[symbol] for symbol in symbols)
    if total <= limit or total <= 0.0:
        return weights
    scale = limit / total
    return {
        symbol: (weight * scale if symbol in symbols else weight)
        for symbol, weight in weights.items()
    }


def _parent_target(instrument: _Instrument) -> tuple[float, bool]:
    target = _feature_optional(
        instrument,
        (
            "parent_target_weight",
            "q1_det_parent_target_weight",
            "parent_target",
        ),
    )
    if target is None or not 0.0 <= target <= 1.0:
        return instrument.current_weight, False
    return target, True


def _return_feature(instrument: _Instrument, sessions: int) -> float | None:
    return _feature_optional(
        instrument,
        (
            f"total_return_{sessions}",
            f"return_{sessions}",
            f"r{sessions}",
        ),
    )


def _volatility_feature(
    instrument: _Instrument, sessions: int
) -> float | None:
    return _feature_optional(
        instrument,
        (
            f"realized_volatility_{sessions}",
            f"annualized_realized_volatility_{sessions}",
            f"volatility_{sessions}",
        ),
    )


def _moving_average_gap(
    instrument: _Instrument, sessions: int
) -> float | None:
    gap = _feature_optional(
        instrument,
        (
            f"moving_average_gap_{sessions}",
            f"ma_gap_{sessions}",
            f"close_to_sma_gap_{sessions}",
        ),
    )
    if gap is not None:
        return gap
    ratio = _feature_optional(
        instrument,
        (
            f"close_to_moving_average_ratio_{sessions}",
            f"close_to_sma_ratio_{sessions}",
        ),
    )
    return None if ratio is None else ratio - 1.0


def _downside_beta_feature(
    instrument: _Instrument, sessions: int
) -> float | None:
    return _feature_optional(
        instrument,
        (
            f"downside_beta_{sessions}_qqq",
            f"downside_beta_to_qqq_{sessions}",
            f"downside_beta_{sessions}",
        ),
    )


def _downside_observations_feature(
    instrument: _Instrument, sessions: int
) -> float | None:
    return _feature_optional(
        instrument,
        (
            f"downside_observation_count_{sessions}",
            f"qqq_downside_observation_count_{sessions}",
            f"downside_observations_{sessions}",
        ),
    )


def _feature(
    instrument: _Instrument,
    names: tuple[str, ...],
    *,
    default: float,
) -> float:
    value = _feature_optional(instrument, names)
    return default if value is None else value


def _feature_optional(
    instrument: _Instrument, names: tuple[str, ...]
) -> float | None:
    if not instrument.feature_integrity:
        return None
    for name in names:
        if name in instrument.features:
            return instrument.features[name]
    return None


def _first_present(source: dict[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if name in source:
            return source[name]
    return _MISSING


def _required_string(source: dict[str, Any], name: str) -> str:
    value = source.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _required_hash(source: dict[str, Any], name: str) -> str:
    value = _required_string(source, name)
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return value


def _required_number(
    source: dict[str, Any],
    name: str,
    *,
    lower: float,
    upper: float | None = None,
    lower_inclusive: bool = False,
) -> float:
    value = _number(source.get(name))
    if value is None:
        raise ValueError(f"{name} is outside its permitted range")
    lower_invalid = value < lower if lower_inclusive else value <= lower
    if lower_invalid or (upper is not None and value > upper):
        raise ValueError(f"{name} is outside its permitted range")
    return value


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError("timestamp must be a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a UTC offset")
    return parsed.astimezone(UTC)


def _is_nonnegative_integer(value: float) -> bool:
    return value >= 0.0 and value.is_integer()


def _canonical_float(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("non-finite output value")
    return float(format(value, ".12g"))


def _canonical_data(value: object) -> Any:
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        return _canonical_float(value)
    if isinstance(value, list):
        sequence = cast(list[Any], value)
        return [_canonical_data(item) for item in sequence]
    if isinstance(value, dict):
        mapping = cast(dict[Any, Any], value)
        if not all(isinstance(key, str) for key in mapping):
            raise ValueError("canonical object keys must be strings")
        string_mapping = cast(dict[str, Any], mapping)
        return {
            key: _canonical_data(string_mapping[key])
            for key in sorted(string_mapping)
        }
    raise ValueError(f"unsupported canonical value: {type(value).__name__}")


def _canonical_hash(value: object) -> str:
    serialized = json.dumps(
        _canonical_data(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
