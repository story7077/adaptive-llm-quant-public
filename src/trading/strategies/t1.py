from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from trading.domain.contracts import FeatureSnapshot, StrategyForecast
from trading.domain.enums import Horizon
from trading.strategies.common import (
    calibrated_forecast,
    covariance_features,
    feature_values,
    normalized_float_exposure,
    require_feature,
    require_feature_contract,
)
from trading.strategies.models import StrategyForecastContext


@dataclass(frozen=True, slots=True)
class T1Strategy:
    strategy_id: str = "T1"
    strategy_version: str = "1.0.0"
    hypothesis_id: str = "B1_T1_V1"
    raw_signal_definition_version: str = "t1_signal_v1"
    signal_floor: Decimal = Decimal("0.5")
    max_risk_units: Decimal = Decimal("1.5")

    def forecast(
        self,
        feature: FeatureSnapshot,
        *,
        context: StrategyForecastContext,
    ) -> StrategyForecast:
        require_feature_contract(
            feature,
            expected_feature_set_version="t1_features_v1",
            expected_feature_code_version="t1_features_v1",
        )
        values = feature_values(feature)
        raw_signal = require_feature(values, "raw_signal")
        beta = require_feature(values, "beta_60")
        raw_exposure = {
            "SOXX": Decimal("1"),
            "QQQ": -beta,
            "USD_CASH": beta - Decimal("1"),
        }
        covariance = covariance_features(values, ("SOXX", "QQQ"))
        unit_exposure = normalized_float_exposure(raw_exposure, covariance)
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
            signal_eligible=raw_signal > self.signal_floor,
            max_risk_units=self.max_risk_units,
        )
