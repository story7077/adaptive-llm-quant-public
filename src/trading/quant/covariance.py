from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext

from trading.quant.config import CovarianceParameters


class Q1MathError(ValueError):
    """Raised when point-in-time inputs cannot produce a valid Q1 result."""


@dataclass(frozen=True, slots=True)
class CovarianceEstimate:
    symbols: tuple[str, ...]
    matrix: tuple[tuple[Decimal, ...], ...]
    ewma_unannualized: tuple[tuple[Decimal, ...], ...]
    decay_lambda: Decimal
    return_observations: int

    def value(self, left_symbol: str, right_symbol: str) -> Decimal:
        try:
            left_index = self.symbols.index(left_symbol)
            right_index = self.symbols.index(right_symbol)
        except ValueError as exc:
            raise Q1MathError("symbol is absent from covariance estimate") from exc
        return self.matrix[left_index][right_index]

    def variance(self, symbol: str) -> Decimal:
        return self.value(symbol, symbol)

    def as_mapping(self) -> dict[str, dict[str, Decimal]]:
        return {
            left: {
                right: self.matrix[left_index][right_index]
                for right_index, right in enumerate(self.symbols)
            }
            for left_index, left in enumerate(self.symbols)
        }


def ewma_covariance(
    aligned_returns: Mapping[str, Sequence[Decimal]],
    *,
    parameters: CovarianceParameters,
) -> CovarianceEstimate:
    """Calculate the versioned zero-initialized, shrunk EWMA covariance.

    The recurrence starts from a zero matrix and consumes returns oldest first.
    Zero initialization is explicit in configuration rather than an implicit
    implementation choice.
    """

    symbols = tuple(sorted(aligned_returns))
    if len(symbols) != 2:
        raise Q1MathError("q1_math_core_v1 requires exactly two aligned return series")
    lengths = {len(aligned_returns[symbol]) for symbol in symbols}
    if len(lengths) != 1 or not lengths or next(iter(lengths)) == 0:
        raise Q1MathError("covariance returns must be non-empty and aligned")
    if parameters.initialization != "ZERO":
        raise Q1MathError("unsupported covariance initialization")

    rows = tuple(
        tuple(_finite_decimal(value, f"return[{symbol}]") for value in aligned_returns[symbol])
        for symbol in symbols
    )
    observation_count = len(rows[0])
    with localcontext() as context:
        context.prec = parameters.decimal_precision
        decay_lambda = (
            parameters.lambda_base.ln() / Decimal(parameters.half_life_sessions)
        ).exp()
        innovation_weight = Decimal("1") - decay_lambda
        dimension = len(symbols)
        ewma = [[Decimal("0") for _ in range(dimension)] for _ in range(dimension)]
        for observation_index in range(observation_count):
            vector = [rows[index][observation_index] for index in range(dimension)]
            updated = [[Decimal("0") for _ in range(dimension)] for _ in range(dimension)]
            for left in range(dimension):
                for right in range(left, dimension):
                    value = (
                        decay_lambda * ewma[left][right]
                        + innovation_weight * vector[left] * vector[right]
                    )
                    updated[left][right] = value
                    updated[right][left] = value
            ewma = updated

        annualization = Decimal(parameters.annualization_sessions)
        annualized = [
            [value * annualization for value in row]
            for row in ewma
        ]
        shrunk = [
            [
                parameters.full_weight * annualized[left][right]
                + (
                    parameters.diagonal_weight * annualized[left][right]
                    if left == right
                    else Decimal("0")
                )
                for right in range(dimension)
            ]
            for left in range(dimension)
        ]
        for index in range(dimension):
            shrunk[index][index] = max(
                shrunk[index][index],
                parameters.variance_epsilon,
            )

        matrix = tuple(tuple(value for value in row) for row in shrunk)
        ewma_result = tuple(tuple(value for value in row) for row in ewma)
        _validate_symmetric_psd(matrix, parameters.psd_tolerance)
        return CovarianceEstimate(
            symbols=symbols,
            matrix=matrix,
            ewma_unannualized=ewma_result,
            decay_lambda=+decay_lambda,
            return_observations=observation_count,
        )


def ewma_annualized_variance(
    completed_returns: Sequence[Decimal],
    *,
    parameters: CovarianceParameters,
) -> Decimal:
    """Estimate one annualized variance without requiring an aligned peer series.

    B0-VOL consumes only completed-session QQQ returns.  The scalar recurrence
    intentionally shares q1_math_core_v1's configured half-life, zero
    initialization, annualization, diagonal shrinkage, precision, and variance
    floor with :func:`ewma_covariance`.
    """

    if not completed_returns:
        raise Q1MathError("variance returns must be non-empty")
    if parameters.initialization != "ZERO":
        raise Q1MathError("unsupported covariance initialization")
    returns = tuple(
        _finite_decimal(value, "variance return")
        for value in completed_returns
    )
    with localcontext() as context:
        context.prec = parameters.decimal_precision
        decay_lambda = (
            parameters.lambda_base.ln() / Decimal(parameters.half_life_sessions)
        ).exp()
        innovation_weight = Decimal("1") - decay_lambda
        ewma = Decimal("0")
        for value in returns:
            ewma = (
                decay_lambda * ewma
                + innovation_weight * value * value
            )
        annualized = ewma * Decimal(parameters.annualization_sessions)
        shrunk = (
            parameters.full_weight * annualized
            + parameters.diagonal_weight * annualized
        )
        return +max(shrunk, parameters.variance_epsilon)


def portfolio_variance(
    weights: Mapping[str, Decimal],
    covariance: CovarianceEstimate,
) -> Decimal:
    variance = Decimal("0")
    for left in covariance.symbols:
        left_weight = _finite_decimal(weights.get(left, Decimal("0")), f"weight[{left}]")
        for right in covariance.symbols:
            right_weight = _finite_decimal(
                weights.get(right, Decimal("0")),
                f"weight[{right}]",
            )
            variance += left_weight * right_weight * covariance.value(left, right)
    if variance < 0:
        raise Q1MathError("portfolio variance is negative")
    return variance


def is_symmetric_positive_semidefinite(
    covariance: CovarianceEstimate,
    *,
    tolerance: Decimal,
) -> bool:
    try:
        _validate_symmetric_psd(covariance.matrix, tolerance)
    except Q1MathError:
        return False
    return True


def _validate_symmetric_psd(
    matrix: tuple[tuple[Decimal, ...], ...],
    tolerance: Decimal,
) -> None:
    if len(matrix) != 2 or any(len(row) != 2 for row in matrix):
        raise Q1MathError("q1_math_core_v1 covariance must be 2x2")
    if abs(matrix[0][1] - matrix[1][0]) > tolerance:
        raise Q1MathError("covariance matrix is not symmetric")
    if matrix[0][0] < -tolerance or matrix[1][1] < -tolerance:
        raise Q1MathError("covariance diagonal is negative")
    determinant = matrix[0][0] * matrix[1][1] - matrix[0][1] * matrix[1][0]
    if determinant < -tolerance:
        raise Q1MathError("covariance matrix is not positive semidefinite")


def _finite_decimal(value: Decimal, name: str) -> Decimal:
    if not value.is_finite():
        raise Q1MathError(f"{name} must be a finite Decimal")
    return value
