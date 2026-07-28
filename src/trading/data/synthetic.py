from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pydantic import Field, field_validator

from trading.domain.contracts import (
    AssetImpactAssessment,
    DomainModel,
    FeatureSnapshot,
    FeatureValue,
    NewsEvent,
    NewsFact,
    PolicyOperation,
    PolicyPatch,
    SourceRecord,
    StrategyForecast,
    TypedCondition,
)
from trading.domain.enums import (
    ComparisonOperator,
    ConditionType,
    EventDirection,
    ExposureKind,
    ForecastStatus,
    Horizon,
    OrdinalBucket,
    PolicyAction,
    PolicyTargetKind,
)
from trading.domain.hashing import canonical_hash, stable_id
from trading.domain.time import require_aware_utc


class SyntheticBar(DomainModel):
    symbol: str
    event_time: datetime
    available_at: datetime
    open: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)

    @field_validator("event_time", "available_at", mode="after")
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)


class SyntheticScenario(DomainModel):
    schema_version: str
    run_id: str
    seed: int
    started_at: datetime
    decision_time: datetime
    initial_cash_usd: Decimal = Field(gt=0)
    execution_scenario_id: str
    commission_rate: Decimal = Field(ge=0)
    commission_waiver_threshold_usd: Decimal = Field(ge=0)
    half_spread_bps: Decimal = Field(ge=0)
    delay_penalty_bps: Decimal = Field(ge=0)
    bars: list[SyntheticBar]

    @field_validator("started_at", "decision_time", mode="after")
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)


def build_demo_scenario() -> SyntheticScenario:
    trading_days = (
        datetime(2026, 7, 13, 19, 44, tzinfo=UTC),
        datetime(2026, 7, 14, 19, 44, tzinfo=UTC),
        datetime(2026, 7, 15, 19, 44, tzinfo=UTC),
        datetime(2026, 7, 16, 19, 44, tzinfo=UTC),
        datetime(2026, 7, 17, 19, 44, tzinfo=UTC),
        datetime(2026, 7, 20, 19, 44, tzinfo=UTC),
        datetime(2026, 7, 21, 19, 44, tzinfo=UTC),
        datetime(2026, 7, 22, 19, 44, tzinfo=UTC),
        datetime(2026, 7, 23, 19, 44, tzinfo=UTC),
        datetime(2026, 7, 24, 19, 44, tzinfo=UTC),
    )
    prices: dict[str, tuple[tuple[str, str], ...]] = {
        "QQQ": (
            ("550.00", "551.20"),
            ("552.00", "553.10"),
            ("552.40", "551.80"),
            ("553.00", "554.50"),
            ("555.00", "556.20"),
            ("557.00", "558.10"),
            ("558.40", "559.00"),
            ("560.00", "558.80"),
            ("559.20", "561.40"),
            ("562.00", "563.10"),
        ),
        "SOXX": (
            ("245.00", "246.10"),
            ("247.00", "248.40"),
            ("247.50", "246.80"),
            ("248.00", "250.20"),
            ("251.00", "252.60"),
            ("253.00", "254.40"),
            ("255.20", "256.00"),
            ("256.50", "254.90"),
            ("255.00", "257.70"),
            ("258.00", "260.10"),
        ),
        "GLD": (
            ("215.00", "215.20"),
            ("215.30", "215.10"),
            ("215.00", "216.00"),
            ("216.10", "216.40"),
            ("216.50", "216.80"),
            ("217.00", "217.30"),
            ("217.40", "217.20"),
            ("217.10", "218.00"),
            ("218.20", "218.50"),
            ("218.60", "219.00"),
        ),
    }
    bars: list[SyntheticBar] = []
    for symbol, series in prices.items():
        for instant, (open_price, close_price) in zip(trading_days, series, strict=True):
            bars.append(
                SyntheticBar(
                    symbol=symbol,
                    event_time=instant,
                    available_at=instant,
                    open=Decimal(open_price),
                    close=Decimal(close_price),
                )
            )
    return SyntheticScenario(
        schema_version="synthetic_scenario_v1",
        run_id="demo_run",
        seed=20260726,
        started_at=datetime(2026, 7, 20, 19, 40, tzinfo=UTC),
        decision_time=datetime(2026, 7, 20, 19, 45, tzinfo=UTC),
        initial_cash_usd=Decimal("100000.00"),
        execution_scenario_id="exec_demo_v1",
        commission_rate=Decimal("0.001"),
        commission_waiver_threshold_usd=Decimal("10.00"),
        half_spread_bps=Decimal("4.0"),
        delay_penalty_bps=Decimal("1.0"),
        bars=bars,
    )


def source_record_for_scenario(scenario: SyntheticScenario) -> SourceRecord:
    source_id = stable_id("src", scenario.run_id, scenario.schema_version)
    return SourceRecord(
        source_id=source_id,
        provider="synthetic",
        external_id=scenario.run_id,
        revision=0,
        content_type="application/vnd.trading.synthetic-scenario+json",
        event_time=scenario.started_at,
        published_at=scenario.started_at,
        available_at=scenario.started_at,
        ingested_at=scenario.started_at,
        revised_at=None,
        content_hash=canonical_hash(scenario),
        raw_object_uri=f"raw://{scenario.run_id}.json",
        license_policy_id="synthetic_internal_v1",
        metadata={
            "schema_version": scenario.schema_version,
            "seed": scenario.seed,
            "bar_count": len(scenario.bars),
        },
    )


def build_feature_fixtures(
    scenario: SyntheticScenario, source: SourceRecord
) -> list[FeatureSnapshot]:
    cutoff = scenario.decision_time - timedelta(minutes=1)
    fixtures = {
        "SOXX": [("relative_trend_21d", 1.16, "zscore"), ("breadth", 0.68, "fraction")],
        "QQQ": [("liquidity_shock", -2.20, "zscore"), ("spread_bps", 8.0, "bps")],
        "GLD": [("trend_63d", 0.72, "zscore"), ("defensive_score", 0.61, "score")],
    }
    snapshots: list[FeatureSnapshot] = []
    for symbol, values in fixtures.items():
        snapshot_id = stable_id("feat", scenario.run_id, symbol, cutoff)
        feature_values = [
            FeatureValue(
                name=name,
                value=value,
                unit=unit,
                source_record_ids=[source.source_id],
                feature_code_version="synthetic_fixture_v1",
            )
            for name, value, unit in values
        ]
        snapshots.append(
            FeatureSnapshot(
                feature_snapshot_id=snapshot_id,
                symbol=symbol,
                decision_time=scenario.decision_time,
                data_available_cutoff=cutoff,
                feature_set_version="phase0_demo_features_v1",
                values=feature_values,
                input_manifest_hash=canonical_hash(feature_values),
                created_at=scenario.decision_time,
            )
        )
    return snapshots


def build_forecast_fixtures(
    scenario: SyntheticScenario,
    features: list[FeatureSnapshot],
    *,
    code_version: str = "phase0_fixture_test",
) -> list[StrategyForecast]:
    by_symbol = {feature.symbol: feature for feature in features}
    specifications = (
        (
            "T1",
            Horizon.H5D,
            {"SOXX": 1.0, "QQQ": -0.70, "USD_CASH": -0.30},
            18.0,
            3.0,
            "SOXX",
        ),
        (
            "R1",
            Horizon.H4,
            {"QQQ": 1.0, "USD_CASH": -1.0},
            9.0,
            2.5,
            "QQQ",
        ),
        (
            "X1",
            Horizon.H5D,
            {"GLD": 0.60, "QQQ": 0.40, "USD_CASH": -1.0},
            12.0,
            2.0,
            "GLD",
        ),
    )
    forecasts: list[StrategyForecast] = []
    for strategy_id, horizon, exposure, gross, cost, feature_symbol in specifications:
        feature = by_symbol[feature_symbol]
        forecasts.append(
            StrategyForecast(
                forecast_id=stable_id("fcst", scenario.run_id, strategy_id),
                hypothesis_id=f"B1_{strategy_id}_V1",
                strategy_id=strategy_id,
                strategy_version="fixture_v1",
                experiment_version="phase0_demo_v1",
                decision_time=scenario.decision_time,
                data_available_cutoff=feature.data_available_cutoff,
                horizon=horizon,
                expires_at=scenario.decision_time
                + (timedelta(hours=4) if horizon is Horizon.H4 else timedelta(days=7)),
                reference_portfolio_id="B0_VOL_V1",
                exposure_kind=ExposureKind.ACTIVE_DELTA,
                unit_exposure=exposure,
                risk_unit_horizon_vol=0.01,
                raw_signal=1.0,
                raw_signal_definition_version="fixture_only_v1",
                expected_gross_return_bps=gross,
                standalone_expected_cost_bps=cost,
                expected_net_return_bps=gross - cost,
                forecast_error_sd_bps=70.0,
                probability_net_positive=0.55,
                quantile_10_bps=-80.0,
                quantile_50_bps=gross - cost,
                quantile_90_bps=100.0,
                effective_sample_size=30.0,
                calibration_shrinkage=0.50,
                health_multiplier=1.0,
                max_risk_units=0.5,
                capacity_usd=Decimal("1000000"),
                feature_snapshot_ids=[feature.feature_snapshot_id],
                calibration_version="fixture_calibration_v1",
                code_commit=code_version,
                status=ForecastStatus.ACTIVE,
                created_at=scenario.decision_time,
            )
        )
    return forecasts


def build_news_fixture(scenario: SyntheticScenario, source: SourceRecord) -> NewsEvent:
    invalidation = TypedCondition(
        condition_id=stable_id("cond", scenario.run_id, "news_expiry"),
        condition_type=ConditionType.TIME_REACHED,
        field="current_time",
        operator=ComparisonOperator.GTE,
        value=(scenario.decision_time + timedelta(hours=6)).isoformat().replace("+00:00", "Z"),
        evaluation_window=None,
        source_ids=[source.source_id],
    )
    content = {
        "event_type": "SYNTHETIC_SUPPLY_RISK",
        "source_id": source.source_id,
        "as_of": scenario.decision_time,
    }
    return NewsEvent(
        news_event_id=stable_id("news", scenario.run_id),
        schema_version="news_event_v2",
        model_run_id="mock_model_run_phase0",
        as_of=scenario.decision_time,
        data_available_cutoff=scenario.decision_time - timedelta(minutes=1),
        source_event_ids=[source.source_id],
        event_type="SYNTHETIC_SUPPLY_RISK",
        actors=["synthetic_actor"],
        facts=[
            NewsFact(
                statement="Synthetic fixture reports a temporary semiconductor supply risk.",
                source_id=source.source_id,
                certainty=1.0,
                is_official_source=True,
            )
        ],
        impacts=[
            AssetImpactAssessment(
                symbol_or_factor="SEMICONDUCTOR_BETA",
                direction=EventDirection.NEGATIVE,
                severity_bucket=OrdinalBucket.MEDIUM,
                horizon=Horizon.H5D,
                transmission_channels=["SUPPLY_CHAIN"],
                raw_confidence=0.80,
            )
        ],
        novelty_bucket=OrdinalBucket.MEDIUM,
        contradiction_source_ids=[],
        invalidation_conditions=[invalidation],
        expires_at=scenario.decision_time + timedelta(hours=6),
        prompt_hash=canonical_hash("phase0_mock_prompt"),
        context_manifest_hash=canonical_hash(content),
        output_hash=canonical_hash(content),
        created_at=scenario.decision_time,
    )


def build_policy_patch_fixture(
    scenario: SyntheticScenario, news_event: NewsEvent
) -> PolicyPatch:
    rollback = TypedCondition(
        condition_id=stable_id("cond", scenario.run_id, "patch_expiry"),
        condition_type=ConditionType.TIME_REACHED,
        field="current_time",
        operator=ComparisonOperator.GTE,
        value=(scenario.decision_time + timedelta(hours=4)).isoformat().replace("+00:00", "Z"),
        evaluation_window=None,
        source_ids=news_event.source_event_ids,
    )
    return PolicyPatch(
        patch_id=stable_id("patch", scenario.run_id, "B3-RISK"),
        schema_version="policy_patch_v2",
        arm_scope="B3-RISK",
        base_policy_version=0,
        effective_from=scenario.decision_time,
        expires_at=scenario.decision_time + timedelta(hours=4),
        operations=[
            PolicyOperation(
                action=PolicyAction.REDUCE_RISK_BUDGET,
                target_kind=PolicyTargetKind.PORTFOLIO,
                target_id="TOTAL",
                risk_budget_delta=None,
                risk_multiplier=0.75,
                blocked=None,
            )
        ],
        evidence_news_event_ids=[news_event.news_event_id],
        raw_confidence=0.80,
        rollback_conditions=[rollback],
        model_run_id=news_event.model_run_id,
        prompt_hash=canonical_hash("phase0_mock_controller_prompt"),
        context_manifest_hash=canonical_hash(
            {"news_event_id": news_event.news_event_id, "arm": "B3-RISK"}
        ),
        created_at=scenario.decision_time,
    )
