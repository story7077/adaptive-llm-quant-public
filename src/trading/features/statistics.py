from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal


class StatisticsError(ValueError):
    """Raised when a statistic is undefined for the supplied point-in-time data."""


def mean(values: Sequence[Decimal]) -> Decimal:
    if not values:
        raise StatisticsError("mean requires at least one value")
    return sum(values, Decimal("0")) / Decimal(len(values))


def median(values: Sequence[Decimal]) -> Decimal:
    if not values:
        raise StatisticsError("median requires at least one value")
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal("2")


def sample_variance(values: Sequence[Decimal]) -> Decimal:
    if len(values) < 2:
        raise StatisticsError("sample variance requires at least two values")
    center = mean(values)
    return sum(((value - center) ** 2 for value in values), Decimal("0")) / Decimal(len(values) - 1)


def sample_std(values: Sequence[Decimal]) -> Decimal:
    variance = sample_variance(values)
    if variance <= 0:
        raise StatisticsError("sample standard deviation is zero")
    return variance.sqrt()


def covariance(left: Sequence[Decimal], right: Sequence[Decimal]) -> Decimal:
    if len(left) != len(right) or len(left) < 2:
        raise StatisticsError("covariance requires aligned samples")
    left_mean = mean(left)
    right_mean = mean(right)
    return sum(
        (
            (left_value - left_mean) * (right_value - right_mean)
            for left_value, right_value in zip(left, right, strict=True)
        ),
        Decimal("0"),
    ) / Decimal(len(left) - 1)


def robust_z(
    value: Decimal,
    history: Sequence[Decimal],
    *,
    clip: Decimal = Decimal("3"),
) -> Decimal:
    if len(history) < 2:
        raise StatisticsError("robust z-score requires history")
    center = median(history)
    mad = median([abs(item - center) for item in history])
    if mad == 0:
        raise StatisticsError("robust z-score MAD is zero")
    score = (value - center) / (Decimal("1.4826") * mad)
    return max(-clip, min(clip, score))


def simple_return(start: Decimal, end: Decimal) -> Decimal:
    if start <= 0:
        raise StatisticsError("return start price must be positive")
    return end / start - Decimal("1")


def ols_slope(left: Sequence[Decimal], right: Sequence[Decimal]) -> Decimal:
    """OLS slope for ``left = alpha + beta * right``."""

    variance = covariance(right, right)
    if variance <= 0:
        raise StatisticsError("OLS regressor variance is zero")
    return covariance(left, right) / variance


def ols_coefficients(
    dependent: Sequence[Decimal],
    regressors: Sequence[Sequence[Decimal]],
) -> tuple[Decimal, list[Decimal]]:
    """Small deterministic OLS solver with an intercept.

    R1 has at most four common factors, so normal equations with partial-pivot
    Gaussian elimination are sufficient and avoid a hidden numpy dependency.
    Singular designs are rejected instead of regularized into a new strategy.
    """

    if len(dependent) < 3:
        raise StatisticsError("OLS requires at least three observations")
    if not regressors:
        return mean(dependent), []
    if any(len(column) != len(dependent) for column in regressors):
        raise StatisticsError("OLS regressors must align with dependent observations")

    columns = [[Decimal("1")] * len(dependent), *[list(column) for column in regressors]]
    width = len(columns)
    matrix: list[list[Decimal]] = []
    for row_index in range(width):
        row = [
            sum(
                (
                    columns[row_index][sample] * columns[column_index][sample]
                    for sample in range(len(dependent))
                ),
                Decimal("0"),
            )
            for column_index in range(width)
        ]
        row.append(
            sum(
                (
                    columns[row_index][sample] * dependent[sample]
                    for sample in range(len(dependent))
                ),
                Decimal("0"),
            )
        )
        matrix.append(row)

    solution = _gaussian_elimination(matrix)
    return solution[0], solution[1:]


def _gaussian_elimination(matrix: list[list[Decimal]]) -> list[Decimal]:
    size = len(matrix)
    scale = max((abs(value) for row in matrix for value in row[:-1]), default=Decimal("0"))
    tolerance = max(Decimal("1e-28"), scale * Decimal("1e-24"))
    for pivot_index in range(size):
        pivot_row = max(
            range(pivot_index, size),
            key=lambda row_index: abs(matrix[row_index][pivot_index]),
        )
        if abs(matrix[pivot_row][pivot_index]) <= tolerance:
            raise StatisticsError("OLS design matrix is singular")
        matrix[pivot_index], matrix[pivot_row] = matrix[pivot_row], matrix[pivot_index]
        pivot = matrix[pivot_index][pivot_index]
        matrix[pivot_index] = [value / pivot for value in matrix[pivot_index]]
        for row_index in range(size):
            if row_index == pivot_index:
                continue
            factor = matrix[row_index][pivot_index]
            if factor == 0:
                continue
            matrix[row_index] = [
                current - factor * pivot_value
                for current, pivot_value in zip(matrix[row_index], matrix[pivot_index], strict=True)
            ]
    return [matrix[index][-1] for index in range(size)]


def portfolio_variance(
    exposure: Mapping[str, Decimal],
    covariance_matrix: Mapping[str, Mapping[str, Decimal]],
) -> Decimal:
    risky = {symbol: weight for symbol, weight in exposure.items() if symbol != "USD_CASH"}
    variance = Decimal("0")
    for left_symbol, left_weight in risky.items():
        if left_symbol not in covariance_matrix:
            raise StatisticsError(f"missing covariance row for {left_symbol}")
        for right_symbol, right_weight in risky.items():
            try:
                covariance_value = covariance_matrix[left_symbol][right_symbol]
            except KeyError as exc:
                raise StatisticsError(
                    f"missing covariance value for {left_symbol}/{right_symbol}"
                ) from exc
            variance += left_weight * right_weight * covariance_value
    if variance <= 0:
        raise StatisticsError("portfolio variance must be positive")
    return variance


def normalize_active_exposure(
    exposure: Mapping[str, Decimal],
    covariance_matrix: Mapping[str, Mapping[str, Decimal]],
    *,
    target_volatility: Decimal = Decimal("0.01"),
) -> dict[str, Decimal]:
    variance = portfolio_variance(exposure, covariance_matrix)
    scale = target_volatility / variance.sqrt()
    normalized = {
        symbol: weight * scale for symbol, weight in exposure.items() if symbol != "USD_CASH"
    }
    normalized["USD_CASH"] = -sum(normalized.values(), Decimal("0"))
    return normalized


def covariance_matrix(
    returns: Mapping[str, Sequence[Decimal]],
    *,
    horizon_periods: int,
) -> dict[str, dict[str, Decimal]]:
    if horizon_periods <= 0:
        raise StatisticsError("horizon_periods must be positive")
    symbols = sorted(returns)
    if not symbols:
        raise StatisticsError("covariance matrix requires returns")
    lengths = {len(returns[symbol]) for symbol in symbols}
    if len(lengths) != 1 or next(iter(lengths)) < 2:
        raise StatisticsError("covariance returns must be aligned")
    multiplier = Decimal(horizon_periods)
    return {
        left: {right: covariance(returns[left], returns[right]) * multiplier for right in symbols}
        for left in symbols
    }
