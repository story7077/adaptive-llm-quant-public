"""Pure mathematical components for the versioned q1_math_core_v1 path."""

from trading.quant.allocator import (
    AllocationResult,
    CurrentPortfolioWeights,
    OmittedOrderDiagnostic,
    ProposedTrade,
    TurnoverResult,
    allocate_b0_vol,
    allocate_q1,
    apply_turnover_control,
    compute_current_weights,
)
from trading.quant.config import Q1MathConfig, parse_q1_math_config
from trading.quant.covariance import (
    CovarianceEstimate,
    Q1MathError,
    ewma_annualized_variance,
    ewma_covariance,
)
from trading.quant.signals import AdjustedCloseObservation, AssetTrend, Q1Signal, compute_q1_signal

__all__ = [
    "AdjustedCloseObservation",
    "AllocationResult",
    "AssetTrend",
    "CovarianceEstimate",
    "CurrentPortfolioWeights",
    "OmittedOrderDiagnostic",
    "ProposedTrade",
    "Q1MathConfig",
    "Q1MathError",
    "Q1Signal",
    "TurnoverResult",
    "allocate_b0_vol",
    "allocate_q1",
    "apply_turnover_control",
    "compute_current_weights",
    "compute_q1_signal",
    "ewma_annualized_variance",
    "ewma_covariance",
    "parse_q1_math_config",
]
