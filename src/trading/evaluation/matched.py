from __future__ import annotations

import random
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from trading.domain.hashing import canonical_hash
from trading.domain.q1 import MatchedComparison, Q1ArmId


class EvaluationError(ValueError):
    """Raised when matched evaluation inputs violate their versioned contract."""


@dataclass(frozen=True, slots=True)
class EvaluationConfig:
    version: str
    decimal_precision: int
    result_quantum: Decimal
    annualization_sessions: int
    risk_free_daily_return: Decimal
    downside_target_daily_return: Decimal
    newey_west_lag: int
    bootstrap_samples: int
    stationary_block_mean_length: Decimal
    bootstrap_confidence: Decimal
    bootstrap_seed: int
    promotion_min_common_sessions: int

    def __post_init__(self) -> None:
        if not self.version:
            raise EvaluationError("Evaluation config version is required")
        if self.decimal_precision <= 0:
            raise EvaluationError("Evaluation decimal precision must be positive")
        if self.result_quantum <= 0:
            raise EvaluationError("Evaluation result quantum must be positive")
        if self.annualization_sessions <= 0:
            raise EvaluationError("Annualization sessions must be positive")
        if self.newey_west_lag < 0:
            raise EvaluationError("Newey-West lag cannot be negative")
        if self.bootstrap_samples <= 0:
            raise EvaluationError("Bootstrap sample count must be positive")
        if self.stationary_block_mean_length < 1:
            raise EvaluationError("Stationary block mean length must be at least one")
        if not Decimal("0") < self.bootstrap_confidence < Decimal("1"):
            raise EvaluationError("Bootstrap confidence must be within (0, 1)")
        if self.promotion_min_common_sessions < 126:
            raise EvaluationError(
                "Promotion threshold cannot be below 126 common sessions"
            )


@dataclass(frozen=True, slots=True)
class DailyEvaluationObservation:
    session_date: date
    arm_id: Q1ArmId
    net_daily_return: Decimal
    daily_turnover: Decimal
    commissions_usd: Decimal
    spread_cost_usd: Decimal
    delay_cost_usd: Decimal
    sensitivity_5bp_usd: Decimal
    sensitivity_10bp_usd: Decimal
    cash_weight: Decimal
    qqq_weight: Decimal
    soxx_weight: Decimal
    risk_episode_active: bool
    llm_reduction_active: bool

    def __post_init__(self) -> None:
        if self.net_daily_return <= Decimal("-1"):
            raise EvaluationError("Daily return cannot lose more than all NAV")
        nonnegative = (
            self.daily_turnover,
            self.commissions_usd,
            self.spread_cost_usd,
            self.delay_cost_usd,
            self.sensitivity_5bp_usd,
            self.sensitivity_10bp_usd,
            self.cash_weight,
            self.qqq_weight,
            self.soxx_weight,
        )
        if any(value < 0 for value in nonnegative):
            raise EvaluationError("Evaluation costs, turnover, and weights cannot be negative")
        if (
            self.cash_weight + self.qqq_weight + self.soxx_weight
            > Decimal("1.0000000001")
        ):
            raise EvaluationError("Evaluation observation cannot imply leverage")
        if self.llm_reduction_active and self.arm_id is not Q1ArmId.Q1_LLM:
            raise EvaluationError("LLM reductions may be attributed only to Q1-LLM")


@dataclass(frozen=True, slots=True)
class PerformanceMetrics:
    arm_id: Q1ArmId
    valid_sessions: int
    cumulative_return: Decimal
    annualized_return: Decimal
    annualized_volatility: Decimal
    sharpe: Decimal | None
    sortino: Decimal | None
    maximum_drawdown: Decimal
    calmar: Decimal | None
    daily_turnover_mean: Decimal
    cumulative_turnover: Decimal
    commissions_usd: Decimal
    spread_and_delay_cost_usd: Decimal
    sensitivity_5bp_usd: Decimal
    sensitivity_10bp_usd: Decimal
    percentage_time_in_cash: Decimal
    qqq_average_exposure: Decimal
    soxx_average_exposure: Decimal
    risk_episode_count: int
    risk_episode_duration_sessions: int
    llm_reduction_count: int
    llm_reduction_duration_sessions: int
    result_hash: str


@dataclass(frozen=True, slots=True)
class MatchedAttribution:
    comparison: MatchedComparison
    left_arm_id: Q1ArmId
    right_arm_id: Q1ArmId
    common_session_dates: tuple[date, ...]
    daily_differences: tuple[Decimal, ...]
    mean_daily_difference: Decimal
    annualized_difference: Decimal
    newey_west_standard_error: Decimal
    bootstrap_lower: Decimal
    bootstrap_upper: Decimal
    common_valid_sessions: int
    eligible_for_manual_promotion_review: bool
    promotion_is_manual: bool
    claims_statistical_significance: bool
    claims_profitability: bool
    result_hash: str


def evaluate_performance(
    observations: Iterable[DailyEvaluationObservation],
    config: EvaluationConfig,
) -> PerformanceMetrics:
    ordered = tuple(sorted(observations, key=lambda item: item.session_date))
    if not ordered:
        raise EvaluationError("Performance evaluation requires observations")
    arm_id = ordered[0].arm_id
    if any(item.arm_id is not arm_id for item in ordered):
        raise EvaluationError("Performance evaluation must contain one arm")
    if len({item.session_date for item in ordered}) != len(ordered):
        raise EvaluationError("Performance sessions must be unique")
    returns = tuple(item.net_daily_return for item in ordered)
    cumulative = _cumulative_return(returns)
    annualized_return = _annualized_return(
        cumulative,
        len(returns),
        config.annualization_sessions,
        config.decimal_precision,
    )
    volatility = _sample_standard_deviation(returns)
    annualized_volatility = volatility * Decimal(config.annualization_sessions).sqrt()
    excess = tuple(value - config.risk_free_daily_return for value in returns)
    excess_mean = _mean(excess)
    sharpe = (
        None
        if volatility == 0
        else excess_mean
        / volatility
        * Decimal(config.annualization_sessions).sqrt()
    )
    downside = tuple(
        min(Decimal("0"), value - config.downside_target_daily_return)
        for value in returns
    )
    downside_deviation = (
        sum((value * value for value in downside), Decimal("0"))
        / Decimal(len(downside))
    ).sqrt()
    sortino = (
        None
        if downside_deviation == 0
        else excess_mean
        / downside_deviation
        * Decimal(config.annualization_sessions).sqrt()
    )
    max_drawdown = _maximum_drawdown(returns)
    calmar = None if max_drawdown == 0 else annualized_return / max_drawdown
    risk_count, risk_duration = _episode_runs(
        tuple(item.risk_episode_active for item in ordered)
    )
    llm_count, llm_duration = _episode_runs(
        tuple(item.llm_reduction_active for item in ordered)
    )
    payload = {
        "arm_id": arm_id,
        "observations": [_observation_payload(item) for item in ordered],
        "evaluation_config": _evaluation_config_payload(config),
        "metrics": {
            "cumulative_return": cumulative,
            "annualized_return": annualized_return,
            "annualized_volatility": annualized_volatility,
            "maximum_drawdown": max_drawdown,
        },
    }
    return PerformanceMetrics(
        arm_id=arm_id,
        valid_sessions=len(ordered),
        cumulative_return=cumulative,
        annualized_return=annualized_return,
        annualized_volatility=annualized_volatility,
        sharpe=sharpe,
        sortino=sortino,
        maximum_drawdown=max_drawdown,
        calmar=calmar,
        daily_turnover_mean=_mean(
            tuple(item.daily_turnover for item in ordered)
        ),
        cumulative_turnover=sum(
            (item.daily_turnover for item in ordered),
            Decimal("0"),
        ),
        commissions_usd=sum(
            (item.commissions_usd for item in ordered),
            Decimal("0"),
        ),
        spread_and_delay_cost_usd=sum(
            (
                item.spread_cost_usd + item.delay_cost_usd
                for item in ordered
            ),
            Decimal("0"),
        ),
        sensitivity_5bp_usd=sum(
            (item.sensitivity_5bp_usd for item in ordered),
            Decimal("0"),
        ),
        sensitivity_10bp_usd=sum(
            (item.sensitivity_10bp_usd for item in ordered),
            Decimal("0"),
        ),
        percentage_time_in_cash=_mean(
            tuple(item.cash_weight for item in ordered)
        ),
        qqq_average_exposure=_mean(
            tuple(item.qqq_weight for item in ordered)
        ),
        soxx_average_exposure=_mean(
            tuple(item.soxx_weight for item in ordered)
        ),
        risk_episode_count=risk_count,
        risk_episode_duration_sessions=risk_duration,
        llm_reduction_count=llm_count,
        llm_reduction_duration_sessions=llm_duration,
        result_hash=canonical_hash(payload),
    )


def evaluate_matched_attribution(
    *,
    comparison: MatchedComparison,
    left_observations: Iterable[DailyEvaluationObservation],
    right_observations: Iterable[DailyEvaluationObservation],
    config: EvaluationConfig,
) -> MatchedAttribution:
    expected_left, expected_right = _MATCHED_ARMS[comparison]
    left = _unique_by_session(left_observations, expected_left)
    right = _unique_by_session(right_observations, expected_right)
    common_dates = tuple(sorted(set(left) & set(right)))
    if not common_dates:
        raise EvaluationError("Matched attribution has no common valid sessions")
    differences = tuple(
        _result_value(
            left[session_date].net_daily_return
            - right[session_date].net_daily_return,
            config,
        )
        for session_date in common_dates
    )
    mean_difference = _result_value(_mean(differences), config)
    annualized_difference = _result_value(
        mean_difference * Decimal(config.annualization_sessions),
        config,
    )
    newey_west = _result_value(
        _newey_west_standard_error(
            differences,
            lag=min(config.newey_west_lag, len(differences) - 1),
        ),
        config,
    )
    lower, upper = _stationary_block_bootstrap_interval(
        differences,
        config=config,
    )
    lower = _result_value(lower, config)
    upper = _result_value(upper, config)
    common_count = len(common_dates)
    payload = {
        "comparison": comparison,
        "left_arm_id": expected_left,
        "right_arm_id": expected_right,
        "common_session_dates": [
            session_date.isoformat() for session_date in common_dates
        ],
        "daily_differences": differences,
        "evaluation_config": _evaluation_config_payload(config),
    }
    return MatchedAttribution(
        comparison=comparison,
        left_arm_id=expected_left,
        right_arm_id=expected_right,
        common_session_dates=common_dates,
        daily_differences=differences,
        mean_daily_difference=mean_difference,
        annualized_difference=annualized_difference,
        newey_west_standard_error=newey_west,
        bootstrap_lower=lower,
        bootstrap_upper=upper,
        common_valid_sessions=common_count,
        eligible_for_manual_promotion_review=(
            common_count >= config.promotion_min_common_sessions
        ),
        promotion_is_manual=True,
        claims_statistical_significance=False,
        claims_profitability=False,
        result_hash=canonical_hash(payload),
    )


def _newey_west_standard_error(
    values: tuple[Decimal, ...],
    *,
    lag: int,
) -> Decimal:
    count = len(values)
    if count == 0:
        raise EvaluationError("Newey-West calculation requires observations")
    mean = _mean(values)
    centered = tuple(value - mean for value in values)
    denominator = Decimal(count)
    long_run_variance = sum(
        (value * value for value in centered),
        Decimal("0"),
    ) / denominator
    for offset in range(1, lag + 1):
        covariance = sum(
            (
                centered[index] * centered[index - offset]
                for index in range(offset, count)
            ),
            Decimal("0"),
        ) / denominator
        bartlett_weight = Decimal(lag + 1 - offset) / Decimal(lag + 1)
        long_run_variance += Decimal("2") * bartlett_weight * covariance
    return (max(Decimal("0"), long_run_variance) / denominator).sqrt()


def _result_value(value: Decimal, config: EvaluationConfig) -> Decimal:
    with localcontext() as context:
        context.prec = config.decimal_precision
        return value.quantize(
            config.result_quantum,
            rounding=ROUND_HALF_EVEN,
        )


def _stationary_block_bootstrap_interval(
    values: tuple[Decimal, ...],
    *,
    config: EvaluationConfig,
) -> tuple[Decimal, Decimal]:
    count = len(values)
    if count == 0:
        raise EvaluationError("Bootstrap requires observations")
    generator = random.Random(config.bootstrap_seed)
    restart_probability = Decimal("1") / config.stationary_block_mean_length
    means: list[Decimal] = []
    for _ in range(config.bootstrap_samples):
        index = generator.randrange(count)
        sample: list[Decimal] = []
        for position in range(count):
            if position > 0:
                if Decimal(str(generator.random())) < restart_probability:
                    index = generator.randrange(count)
                else:
                    index = (index + 1) % count
            sample.append(values[index])
        means.append(_mean(tuple(sample)))
    means.sort()
    tail = (Decimal("1") - config.bootstrap_confidence) / Decimal("2")
    lower = _percentile(means, tail)
    upper = _percentile(means, Decimal("1") - tail)
    return lower, upper


def _percentile(sorted_values: list[Decimal], probability: Decimal) -> Decimal:
    if not sorted_values:
        raise EvaluationError("Percentile requires values")
    if not Decimal("0") <= probability <= Decimal("1"):
        raise EvaluationError("Percentile probability must be within [0, 1]")
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = probability * Decimal(len(sorted_values) - 1)
    lower_index = int(rank)
    upper_index = min(lower_index + 1, len(sorted_values) - 1)
    fraction = rank - Decimal(lower_index)
    return (
        sorted_values[lower_index] * (Decimal("1") - fraction)
        + sorted_values[upper_index] * fraction
    )


def _unique_by_session(
    observations: Iterable[DailyEvaluationObservation],
    expected_arm: Q1ArmId,
) -> dict[date, DailyEvaluationObservation]:
    result: dict[date, DailyEvaluationObservation] = {}
    for observation in observations:
        if observation.arm_id is not expected_arm:
            raise EvaluationError(
                f"Expected {expected_arm}, received {observation.arm_id}"
            )
        if observation.session_date in result:
            raise EvaluationError("Matched arm has duplicate session")
        result[observation.session_date] = observation
    return result


def _cumulative_return(returns: tuple[Decimal, ...]) -> Decimal:
    wealth = Decimal("1")
    for value in returns:
        wealth *= Decimal("1") + value
    return wealth - Decimal("1")


def _annualized_return(
    cumulative_return: Decimal,
    session_count: int,
    annualization_sessions: int,
    decimal_precision: int,
) -> Decimal:
    if session_count <= 0:
        raise EvaluationError("Annualized return requires sessions")
    gross = Decimal("1") + cumulative_return
    if gross <= 0:
        return Decimal("-1")
    with localcontext() as context:
        context.prec = decimal_precision
        exponent = Decimal(annualization_sessions) / Decimal(session_count)
        return (gross.ln() * exponent).exp() - Decimal("1")


def _sample_standard_deviation(values: tuple[Decimal, ...]) -> Decimal:
    if len(values) < 2:
        return Decimal("0")
    mean = _mean(values)
    variance = sum(
        ((value - mean) ** 2 for value in values),
        Decimal("0"),
    ) / Decimal(len(values) - 1)
    return variance.sqrt()


def _maximum_drawdown(returns: tuple[Decimal, ...]) -> Decimal:
    wealth = Decimal("1")
    peak = wealth
    maximum = Decimal("0")
    for value in returns:
        wealth *= Decimal("1") + value
        peak = max(peak, wealth)
        maximum = max(maximum, (peak - wealth) / peak)
    return maximum


def _mean(values: tuple[Decimal, ...]) -> Decimal:
    if not values:
        raise EvaluationError("Mean requires observations")
    return sum(values, Decimal("0")) / Decimal(len(values))


def _episode_runs(flags: tuple[bool, ...]) -> tuple[int, int]:
    count = 0
    duration = 0
    active = False
    for flag in flags:
        if flag:
            duration += 1
            if not active:
                count += 1
                active = True
        else:
            active = False
    return count, duration


def _observation_payload(
    observation: DailyEvaluationObservation,
) -> dict[str, object]:
    return {
        "session_date": observation.session_date,
        "arm_id": observation.arm_id,
        "net_daily_return": observation.net_daily_return,
        "daily_turnover": observation.daily_turnover,
        "commissions_usd": observation.commissions_usd,
        "spread_cost_usd": observation.spread_cost_usd,
        "delay_cost_usd": observation.delay_cost_usd,
        "sensitivity_5bp_usd": observation.sensitivity_5bp_usd,
        "sensitivity_10bp_usd": observation.sensitivity_10bp_usd,
        "cash_weight": observation.cash_weight,
        "qqq_weight": observation.qqq_weight,
        "soxx_weight": observation.soxx_weight,
        "risk_episode_active": observation.risk_episode_active,
        "llm_reduction_active": observation.llm_reduction_active,
    }


def _evaluation_config_payload(config: EvaluationConfig) -> dict[str, object]:
    return {
        "version": config.version,
        "decimal_precision": config.decimal_precision,
        "annualization_sessions": config.annualization_sessions,
        "risk_free_daily_return": config.risk_free_daily_return,
        "downside_target_daily_return": config.downside_target_daily_return,
        "newey_west_lag": config.newey_west_lag,
        "bootstrap_samples": config.bootstrap_samples,
        "stationary_block_mean_length": config.stationary_block_mean_length,
        "bootstrap_confidence": config.bootstrap_confidence,
        "bootstrap_seed": config.bootstrap_seed,
        "promotion_min_common_sessions": config.promotion_min_common_sessions,
    }


_MATCHED_ARMS = {
    MatchedComparison.Q1_DET_MINUS_B0_VOL: (
        Q1ArmId.Q1_DET,
        Q1ArmId.B0_VOL,
    ),
    MatchedComparison.Q1_LLM_MINUS_Q1_DET: (
        Q1ArmId.Q1_LLM,
        Q1ArmId.Q1_DET,
    ),
}
