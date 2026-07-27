from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal

from trading.features.models import (
    AdjustedPriceObservation,
    FeatureBuildContext,
    FeatureBuildResult,
    IndexMembership,
    blocked_result,
    feature_snapshot,
)
from trading.features.pit import (
    NEW_YORK,
    common_sessions,
    historical_cutoff,
    select_session_marks,
)
from trading.features.statistics import (
    StatisticsError,
    covariance_matrix,
    ols_slope,
    robust_z,
    sample_std,
    simple_return,
)

STRATEGY_ID = "T1"
FEATURE_CODE_VERSION = "t1_features_v1"


@dataclass(frozen=True, slots=True)
class T1FeatureParameters:
    lookback_fast: int = 20
    lookback_slow: int = 60
    breadth_delta_days: int = 10
    normalizer_days: int = 252
    beta_days: int = 60
    risk_history_days: int = 60
    horizon_days: int = 5
    min_constituents: int = 10
    min_constituent_coverage: Decimal = Decimal("0.80")

    def __post_init__(self) -> None:
        positive = (
            self.lookback_fast,
            self.lookback_slow,
            self.breadth_delta_days,
            self.normalizer_days,
            self.beta_days,
            self.risk_history_days,
            self.horizon_days,
            self.min_constituents,
        )
        if any(value <= 0 for value in positive):
            raise ValueError("T1 lookbacks and minimum constituent count must be positive")
        if not Decimal("0") < self.min_constituent_coverage <= Decimal("1"):
            raise ValueError("T1 constituent coverage must be in (0, 1]")


def build_t1_features(
    *,
    context: FeatureBuildContext,
    prices: list[AdjustedPriceObservation],
    memberships: list[IndexMembership],
    parameters: T1FeatureParameters | None = None,
) -> FeatureBuildResult:
    """Build the T1 point-in-time relative-trend and breadth signal.

    Historical marks are reconstructed at the current feature-cutoff clock. The
    builder refuses to emit a snapshot when contemporaneous SOXX membership or
    constituent coverage is missing, matching the production research block in
    the design.
    """

    if context.feature_set_version != FEATURE_CODE_VERSION:
        return blocked_result(STRATEGY_ID, None, "T1_FEATURE_VERSION_MISMATCH")
    parameters = parameters or T1FeatureParameters()
    panel = select_session_marks(prices, reference_cutoff=context.data_available_cutoff)
    sessions = common_sessions(panel, ("SOXX", "QQQ"))
    minimum_sessions = (
        max(
            parameters.lookback_slow,
            parameters.beta_days,
            parameters.risk_history_days,
            parameters.breadth_delta_days + 63,
        )
        + parameters.normalizer_days
    )
    if len(sessions) < minimum_sessions:
        return blocked_result(STRATEGY_ID, None, "T1_INSUFFICIENT_PRICE_HISTORY")

    current_session = sessions[-1]
    if current_session != context.data_available_cutoff.astimezone(NEW_YORK).date():
        return blocked_result(STRATEGY_ID, None, "T1_CURRENT_SESSION_MARK_REQUIRED")
    current_members = _members_as_of(
        memberships,
        session_date=current_session,
        cutoff=context.data_available_cutoff,
    )
    if len(current_members) < parameters.min_constituents:
        return blocked_result(STRATEGY_ID, None, "T1_PIT_MEMBERSHIP_REQUIRED")

    source_ids: set[str] = set()

    def price(symbol: str, session: date) -> Decimal:
        try:
            observation = panel[symbol][session]
        except KeyError as exc:
            raise StatisticsError(f"missing price for {symbol} on {session}") from exc
        source_ids.add(observation.source_record_id)
        return observation.adjusted_price

    def active_returns(end_index: int, days: int) -> tuple[list[Decimal], list[Decimal]]:
        if end_index < days:
            raise StatisticsError("insufficient return lookback")
        soxx_returns: list[Decimal] = []
        qqq_returns: list[Decimal] = []
        for index in range(end_index - days + 1, end_index + 1):
            prior_session = sessions[index - 1]
            session = sessions[index]
            soxx_returns.append(simple_return(price("SOXX", prior_session), price("SOXX", session)))
            qqq_returns.append(simple_return(price("QQQ", prior_session), price("QQQ", session)))
        return soxx_returns, qqq_returns

    def relative_strength(end_index: int, days: int) -> Decimal:
        start_session = sessions[end_index - days]
        end_session = sessions[end_index]
        soxx_period_return = simple_return(
            price("SOXX", start_session),
            price("SOXX", end_session),
        )
        qqq_period_return = simple_return(
            price("QQQ", start_session),
            price("QQQ", end_session),
        )
        soxx_returns, qqq_returns = active_returns(end_index, days)
        beta = ols_slope(soxx_returns, qqq_returns)
        residuals = [
            soxx_return - beta * qqq_return
            for soxx_return, qqq_return in zip(
                soxx_returns,
                qqq_returns,
                strict=True,
            )
        ]
        residual_period_vol = sample_std(residuals) * Decimal(days).sqrt()
        return (soxx_period_return - qqq_period_return) / residual_period_vol

    breadth_cache: dict[int, Decimal] = {}

    def breadth(end_index: int) -> Decimal:
        cached = breadth_cache.get(end_index)
        if cached is not None:
            return cached
        session = sessions[end_index]
        cutoff = historical_cutoff(session, context.data_available_cutoff)
        members = _members_as_of(memberships, session_date=session, cutoff=cutoff)
        if len(members) < parameters.min_constituents:
            raise StatisticsError("point-in-time constituent set unavailable")
        for membership in memberships:
            if (
                membership.universe_id == "SOXX"
                and membership.symbol in members
                and membership.available_at <= cutoff
                and membership.is_effective(session)
            ):
                source_ids.add(membership.source_record_id)

        eligible = 0
        above_ma = 0
        positive_5d = 0
        positive_relative_20d = 0
        near_63d_high = 0
        required_sessions = sessions[end_index - 62 : end_index + 1]
        start_5 = sessions[end_index - 5]
        start_20 = sessions[end_index - 20]
        qqq_return_20 = simple_return(price("QQQ", start_20), price("QQQ", session))

        for symbol in sorted(members):
            by_date = panel.get(symbol)
            if by_date is None or any(item not in by_date for item in required_sessions):
                continue
            observations = [by_date[item] for item in required_sessions]
            if any(observation.available_at > cutoff for observation in observations):
                continue
            source_ids.update(observation.source_record_id for observation in observations)
            current = by_date[session].adjusted_price
            recent_20 = [
                by_date[item].adjusted_price for item in sessions[end_index - 19 : end_index + 1]
            ]
            eligible += 1
            above_ma += int(current > sum(recent_20, Decimal("0")) / Decimal("20"))
            positive_5d += int(simple_return(by_date[start_5].adjusted_price, current) > 0)
            positive_relative_20d += int(
                simple_return(by_date[start_20].adjusted_price, current) - qqq_return_20 > 0
            )
            high_63 = max(observation.adjusted_price for observation in observations)
            near_63d_high += int(current >= high_63 * Decimal("0.95"))

        coverage = Decimal(eligible) / Decimal(len(members))
        if eligible == 0 or coverage < parameters.min_constituent_coverage:
            raise StatisticsError("constituent price coverage below threshold")
        result = Decimal(above_ma + positive_5d + positive_relative_20d + near_63d_high) / Decimal(
            eligible * 4
        )
        breadth_cache[end_index] = result
        return result

    component_start = len(sessions) - parameters.normalizer_days
    if component_start <= max(
        parameters.lookback_slow,
        parameters.breadth_delta_days + 63,
    ):
        return blocked_result(STRATEGY_ID, None, "T1_INSUFFICIENT_NORMALIZER_HISTORY")

    rs_fast_history: list[Decimal] = []
    rs_slow_history: list[Decimal] = []
    breadth_delta_history: list[Decimal] = []
    try:
        for end_index in range(component_start, len(sessions)):
            rs_fast_history.append(relative_strength(end_index, parameters.lookback_fast))
            rs_slow_history.append(relative_strength(end_index, parameters.lookback_slow))
            breadth_delta_history.append(
                breadth(end_index) - breadth(end_index - parameters.breadth_delta_days)
            )
        z_fast = robust_z(rs_fast_history[-1], rs_fast_history)
        z_slow = robust_z(rs_slow_history[-1], rs_slow_history)
        z_breadth = robust_z(breadth_delta_history[-1], breadth_delta_history)
        raw_signal = (z_fast + z_slow + z_breadth) / Decimal("3")

        current_index = len(sessions) - 1
        soxx_returns, qqq_returns = active_returns(current_index, parameters.beta_days)
        beta = max(
            Decimal("0.3"),
            min(Decimal("2.0"), ols_slope(soxx_returns, qqq_returns)),
        )
        risk_soxx, risk_qqq = active_returns(
            current_index,
            parameters.risk_history_days,
        )
        risk_covariance = covariance_matrix(
            {"SOXX": risk_soxx, "QQQ": risk_qqq},
            horizon_periods=parameters.horizon_days,
        )
    except StatisticsError:
        return blocked_result(STRATEGY_ID, None, "T1_UNSTABLE_OR_INCOMPLETE_FEATURES")

    lineage = sorted(source_ids)
    values = [
        ("raw_signal", raw_signal, "z_score", lineage),
        ("z_rs_fast", z_fast, "z_score", lineage),
        ("z_rs_slow", z_slow, "z_score", lineage),
        ("z_breadth_delta", z_breadth, "z_score", lineage),
        ("beta_60", beta, "ratio", lineage),
        (
            "cov.SOXX.SOXX",
            risk_covariance["SOXX"]["SOXX"],
            "horizon_return_covariance",
            lineage,
        ),
        (
            "cov.SOXX.QQQ",
            risk_covariance["SOXX"]["QQQ"],
            "horizon_return_covariance",
            lineage,
        ),
        (
            "cov.QQQ.SOXX",
            risk_covariance["QQQ"]["SOXX"],
            "horizon_return_covariance",
            lineage,
        ),
        (
            "cov.QQQ.QQQ",
            risk_covariance["QQQ"]["QQQ"],
            "horizon_return_covariance",
            lineage,
        ),
    ]
    manifest = {
        "strategy_id": STRATEGY_ID,
        "feature_code_version": FEATURE_CODE_VERSION,
        "parameters": asdict(parameters),
        "source_record_ids": lineage,
        "current_session": current_session.isoformat(),
    }
    return feature_snapshot(
        strategy_id=STRATEGY_ID,
        symbol=None,
        context=context,
        feature_code_version=FEATURE_CODE_VERSION,
        values=values,
        manifest=manifest,
    )


def _members_as_of(
    memberships: list[IndexMembership],
    *,
    session_date: date,
    cutoff: datetime,
) -> set[str]:
    return {
        membership.symbol
        for membership in memberships
        if membership.universe_id == "SOXX"
        and membership.available_at <= cutoff
        and membership.is_effective(session_date)
    }
