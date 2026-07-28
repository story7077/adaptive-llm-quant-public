from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from trading.domain.contracts import FeatureSnapshot, StrategyForecast
from trading.domain.enums import Horizon
from trading.strategies.common import (
    StrategyInputError,
    calibrated_forecast,
    feature_values,
    require_feature,
    require_feature_contract,
)
from trading.strategies.models import StrategyForecastContext


@dataclass(frozen=True, slots=True)
class R1Strategy:
    strategy_id: str = "R1"
    strategy_version: str = "1.0.0"
    hypothesis_id: str = "B1_R1_V1"
    raw_signal_definition_version: str = "r1_signal_v1"
    signal_floor: Decimal = Decimal("2.0")
    max_risk_units: Decimal = Decimal("0.5")

    def forecast(
        self,
        feature: FeatureSnapshot,
        *,
        context: StrategyForecastContext,
    ) -> StrategyForecast:
        if feature.symbol is None:
            raise StrategyInputError("R1 feature snapshot requires a target symbol")
        require_feature_contract(
            feature,
            expected_feature_set_version="r1_features_v1",
            expected_feature_code_version="r1_features_v1",
        )
        values = feature_values(feature)
        raw_signal = require_feature(values, "raw_signal")
        horizon_vol = require_feature(values, "horizon_vol")
        if horizon_vol <= 0:
            raise StrategyInputError("R1 horizon volatility must be positive")
        scale = Decimal("0.01") / horizon_vol
        risky = float(scale)
        unit_exposure = {
            feature.symbol: risky,
            "USD_CASH": -risky,
        }
        eligible = raw_signal >= self.signal_floor and require_feature(
            values, "eligible"
        ) == Decimal("1")
        return calibrated_forecast(
            snapshot=feature,
            context=context,
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            hypothesis_id=self.hypothesis_id,
            raw_signal_definition_version=self.raw_signal_definition_version,
            horizon=Horizon.H4,
            unit_exposure=unit_exposure,
            raw_signal=raw_signal,
            signal_eligible=eligible,
            max_risk_units=self.max_risk_units,
        )
