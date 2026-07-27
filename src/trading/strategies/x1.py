from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from trading.domain.contracts import FeatureSnapshot, StrategyForecast
from trading.domain.enums import Horizon
from trading.features.statistics import StatisticsError
from trading.strategies.common import (
    calibrated_forecast,
    covariance_features,
    feature_values,
    normalized_float_exposure,
    require_feature,
    require_feature_contract,
    zero_float_exposure,
)
from trading.strategies.models import StrategyForecastContext

X1_ASSETS = ("SPY", "QQQ", "IWM", "SOXX", "XLK", "HYG", "TLT", "GLD")


@dataclass(frozen=True, slots=True)
class X1Strategy:
    strategy_id: str = "X1"
    strategy_version: str = "1.0.0"
    hypothesis_id: str = "B1_X1_V1"
    raw_signal_definition_version: str = "x1_signal_v1"
    max_risk_units: Decimal = Decimal("1.5")

    def forecast(
        self,
        feature: FeatureSnapshot,
        *,
        context: StrategyForecastContext,
    ) -> StrategyForecast:
        require_feature_contract(
            feature,
            expected_feature_set_version="x1_features_v1",
            expected_feature_code_version="x1_features_v1",
        )
        values = feature_values(feature)
        raw_signal = require_feature(values, "raw_signal")
        raw_exposure = {
            symbol: require_feature(values, f"active_delta.{symbol}")
            for symbol in (*X1_ASSETS, "USD_CASH")
        }
        has_active_delta = any(
            weight != 0 for symbol, weight in raw_exposure.items() if symbol != "USD_CASH"
        )
        if has_active_delta:
            covariance = covariance_features(values, X1_ASSETS)
            try:
                unit_exposure = normalized_float_exposure(
                    raw_exposure,
                    covariance,
                )
            except StatisticsError:
                has_active_delta = False
                unit_exposure = zero_float_exposure(X1_ASSETS)
        else:
            unit_exposure = zero_float_exposure(X1_ASSETS)

        return calibrated_forecast(
            snapshot=feature,
            context=context,
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            hypothesis_id=self.hypothesis_id,
            raw_signal_definition_version=self.raw_signal_definition_version,
            horizon=Horizon.H5D,
            unit_exposure=unit_exposure,
            raw_signal=raw_signal,
            signal_eligible=raw_signal > 0 and has_active_delta,
            max_risk_units=self.max_risk_units,
        )
