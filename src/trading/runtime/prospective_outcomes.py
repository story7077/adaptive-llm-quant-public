from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import desc, select
from sqlalchemy.orm import Session, sessionmaker

from trading.data.alpaca import FEED, PROVIDER
from trading.data.market_repository import MarketDataRepository
from trading.domain.time import require_aware_utc
from trading.persistence.models import (
    MarketBarRow,
    MarketCalendarSessionRow,
    StrategyEvaluationAnchorRow,
)
from trading.persistence.prospective import ProspectiveCandidateRepository
from trading.persistence.prospective_outcomes import (
    ProspectiveOutcomeRepository,
)
from trading.research.evaluation_contracts import KnownFactorReturnV1
from trading.research.prospective import (
    ProspectiveRequestEvidenceV1,
)
from trading.research.prospective_outcomes import (
    ProspectiveOutcomeConfigBundle,
    ProspectiveOutcomeError,
    ProspectiveOutcomeEvidenceV1,
    ProspectiveOutcomeFailureV1,
    ProspectiveOutcomeSourceBarV1,
    build_prospective_outcome_evidence,
    build_prospective_outcome_failure,
)

NEW_YORK = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class ProspectiveOutcomeCollectionResult:
    evidence: ProspectiveOutcomeEvidenceV1 | None
    failure: ProspectiveOutcomeFailureV1 | None
    created: bool

    def __post_init__(self) -> None:
        if (self.evidence is None) == (self.failure is None):
            raise ValueError(
                "prospective outcome result requires one terminal artifact"
            )


class ProspectiveOutcomeCollector:
    """Materialize one immutable next-close forward outcome in order."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        config: ProspectiveOutcomeConfigBundle,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._outcomes = ProspectiveOutcomeRepository(session_factory)
        self._prospective = ProspectiveCandidateRepository(session_factory)
        self._market = MarketDataRepository(session_factory)

    def collect_next(
        self,
        *,
        challenger_id: str,
        as_of: datetime | None = None,
    ) -> ProspectiveOutcomeCollectionResult:
        pending = self._outcomes.next_pending(
            challenger_id=challenger_id
        )
        if pending is None:
            raise ProspectiveOutcomeError(
                "PROSPECTIVE_OUTCOME_REQUEST_NOT_AVAILABLE"
            )
        instant = (
            self._outcomes.database_clock()
            if as_of is None
            else require_aware_utc(as_of, "as_of")
        )
        request = pending.request
        execution = pending.execution
        implementation, evaluation = self._future_sessions(request)
        evaluation_close = _utc(evaluation.close_at)
        if instant <= evaluation_close:
            raise ProspectiveOutcomeError(
                "PROSPECTIVE_OUTCOME_NOT_YET_AVAILABLE"
            )
        outcome_data_cutoff = evaluation_close + timedelta(
            minutes=(
                self._config.config.implementation
                .outcome_data_delay_minutes
            )
        )
        if instant <= outcome_data_cutoff:
            self._outcome_bars(
                request=request,
                implementation_date=implementation.session_date,
                evaluation_date=evaluation.session_date,
                as_of=instant,
            )
            raise ProspectiveOutcomeError(
                "PROSPECTIVE_OUTCOME_NOT_YET_FINALIZED"
            )
        try:
            bars = self._outcome_bars(
                request=request,
                implementation_date=implementation.session_date,
                evaluation_date=evaluation.session_date,
                as_of=outcome_data_cutoff,
            )
        except ProspectiveOutcomeError as exc:
            if str(exc) == "PROSPECTIVE_OUTCOME_BAR_NOT_AVAILABLE":
                failure = build_prospective_outcome_failure(
                    request=request,
                    execution=execution,
                    config=self._config,
                    implementation_calendar_session_id=(
                        implementation.calendar_session_id
                    ),
                    evaluation_calendar_session_id=(
                        evaluation.calendar_session_id
                    ),
                    outcome_data_cutoff=outcome_data_cutoff,
                )
                return ProspectiveOutcomeCollectionResult(
                    evidence=None,
                    failure=failure,
                    created=self._outcomes.store_failure(failure),
                )
            raise
        candidate_current = {
            item.symbol: item.current_weight
            for item in request.request.instruments
        }
        response = execution.primary_response
        if response is None:
            raise ProspectiveOutcomeError(
                "PROSPECTIVE_OUTCOME_EXECUTION_BINDING_INVALID"
            )
        candidate_target = {
            item.symbol: item.target_weight for item in response.targets
        }
        baseline_current = self._baseline_current(request)
        baseline_target = self._parent_targets(request)
        forward = {
            symbol: _simple_return(
                bars[(implementation.session_date, symbol)],
                bars[(evaluation.session_date, symbol)],
            )
            for symbol in candidate_current
        }
        market_symbol = self._config.config.market_context.market_symbol
        sector_symbol = self._config.config.market_context.sector_symbol
        market_return = _simple_return(
            bars[(implementation.session_date, market_symbol)],
            bars[(evaluation.session_date, market_symbol)],
        )
        sector_return = _simple_return(
            bars[(implementation.session_date, sector_symbol)],
            bars[(evaluation.session_date, sector_symbol)],
        )
        known_factors = tuple(
            KnownFactorReturnV1(
                factor_id=factor.factor_id,
                return_value=(
                    _simple_return(
                        bars[
                            (
                                implementation.session_date,
                                factor.long_symbol,
                            )
                        ],
                        bars[
                            (
                                evaluation.session_date,
                                factor.long_symbol,
                            )
                        ],
                    )
                    - _simple_return(
                        bars[
                            (
                                implementation.session_date,
                                factor.short_symbol,
                            )
                        ],
                        bars[
                            (
                                evaluation.session_date,
                                factor.short_symbol,
                            )
                        ],
                    )
                ),
            )
            for factor in self._config.config.market_context.known_factors
        )
        source_bars = tuple(
            _source_bar(row)
            for _, row in sorted(
                bars.items(),
                key=lambda item: (
                    item[0][0],
                    item[0][1],
                    item[1].bar_id,
                ),
            )
        )
        evidence = build_prospective_outcome_evidence(
            request=request,
            execution=execution,
            config=self._config,
            implementation_calendar_session_id=(
                implementation.calendar_session_id
            ),
            evaluation_calendar_session_id=(
                evaluation.calendar_session_id
            ),
            implementation_close_at=_utc(implementation.close_at),
            evaluation_close_at=evaluation_close,
            outcome_data_cutoff=outcome_data_cutoff,
            evaluation_nav_usd=self._evaluation_nav(request),
            candidate_current_weights=candidate_current,
            candidate_target_weights=candidate_target,
            baseline_current_weights=baseline_current,
            baseline_target_weights=baseline_target,
            forward_returns=forward,
            adv_usd=self._adv_usd(request),
            market_return=market_return,
            sector_return=sector_return,
            known_factor_returns=known_factors,
            regime=self._regime(market_return),
            source_bars=source_bars,
            created_at=outcome_data_cutoff,
        )
        return ProspectiveOutcomeCollectionResult(
            evidence=evidence,
            failure=None,
            created=self._outcomes.store(evidence),
        )

    def _future_sessions(
        self,
        request: ProspectiveRequestEvidenceV1,
    ) -> tuple[MarketCalendarSessionRow, MarketCalendarSessionRow]:
        with self._session_factory() as session:
            decision = session.get(
                MarketCalendarSessionRow,
                request.calendar_session_id,
            )
            if decision is None:
                raise ProspectiveOutcomeError(
                    "PROSPECTIVE_OUTCOME_DECISION_CALENDAR_INVALID"
                )
            if (
                decision.calendar_version
                != self._config.config.calendar_version
                or _utc(decision.available_at)
                > request.request.decision_time
            ):
                raise ProspectiveOutcomeError(
                    "PROSPECTIVE_OUTCOME_DECISION_CALENDAR_NOT_POINT_IN_TIME"
                )
            rows = tuple(
                session.scalars(
                    select(MarketCalendarSessionRow)
                    .where(
                        MarketCalendarSessionRow.calendar_version
                        == self._config.config.calendar_version,
                        MarketCalendarSessionRow.session_date
                        > decision.session_date,
                        MarketCalendarSessionRow.available_at
                        <= request.request.decision_time,
                    )
                    .order_by(
                        MarketCalendarSessionRow.session_date,
                        desc(MarketCalendarSessionRow.available_at),
                        desc(MarketCalendarSessionRow.calendar_session_id),
                    )
                )
            )
        selected: list[MarketCalendarSessionRow] = []
        seen: set[date] = set()
        for row in rows:
            if row.session_date in seen:
                continue
            selected.append(row)
            seen.add(row.session_date)
            if len(selected) == (
                self._config.config.implementation.delay_sessions
                + self._config.config.implementation.return_horizon_sessions
            ):
                break
        if len(selected) != 2:
            raise ProspectiveOutcomeError(
                "PROSPECTIVE_OUTCOME_CALENDAR_NOT_POINT_IN_TIME"
            )
        return selected[0], selected[1]

    def _outcome_bars(
        self,
        *,
        request: ProspectiveRequestEvidenceV1,
        implementation_date: date,
        evaluation_date: date,
        as_of: datetime,
    ) -> dict[tuple[date, str], MarketBarRow]:
        symbols = set(item.symbol for item in request.request.instruments)
        context = self._config.config.market_context
        symbols.update((context.market_symbol, context.sector_symbol))
        for factor in context.known_factors:
            symbols.update((factor.long_symbol, factor.short_symbol))
        selected: dict[tuple[date, str], MarketBarRow] = {}
        for symbol in sorted(symbols):
            rows = self._market.latest_bars(
                provider=PROVIDER,
                feed=FEED,
                symbol=symbol,
                timeframe=self._config.config.timeframe,
                as_of=as_of,
                limit=(
                    self._config.config.operations.outcome_bar_query_limit
                ),
            )
            by_date = {
                _session_date(row): row
                for row in rows
                if (
                    row.payload_json.get("_adjustment")
                    == self._config.config.adjustment
                    and row.payload_json.get("_dataset_version")
                    == self._config.config.market_dataset_version
                )
            }
            for session_date in (
                implementation_date,
                evaluation_date,
            ):
                row = by_date.get(session_date)
                if row is None:
                    raise ProspectiveOutcomeError(
                        "PROSPECTIVE_OUTCOME_BAR_NOT_AVAILABLE"
                    )
                if row.close <= 0 or row.volume < 0:
                    raise ProspectiveOutcomeError(
                        "PROSPECTIVE_OUTCOME_BAR_INVALID"
                    )
                selected[(session_date, symbol)] = row
        return selected

    def _adv_usd(
        self,
        request: ProspectiveRequestEvidenceV1,
    ) -> dict[str, float]:
        lookback = (
            self._config.config.capacity.adv_lookback_completed_sessions
        )
        bar_ids_by_symbol: dict[str, list[str]] = {
            item.symbol: [] for item in request.request.instruments
        }
        for item in request.source_manifest.source_bars:
            if item.symbol in bar_ids_by_symbol:
                bar_ids_by_symbol[item.symbol].append(item.bar_id)
        result: dict[str, float] = {}
        with self._session_factory() as session:
            for symbol, bar_ids in sorted(bar_ids_by_symbol.items()):
                rows = tuple(
                    session.scalars(
                        select(MarketBarRow)
                        .where(MarketBarRow.bar_id.in_(bar_ids))
                        .order_by(MarketBarRow.event_time)
                    )
                )
                if len(rows) < lookback:
                    raise ProspectiveOutcomeError(
                        "PROSPECTIVE_OUTCOME_ADV_HISTORY_INCOMPLETE"
                    )
                values = tuple(
                    row.close * row.volume for row in rows[-lookback:]
                )
                adv = sum(values, Decimal("0")) / Decimal(lookback)
                if adv <= 0:
                    raise ProspectiveOutcomeError(
                        "PROSPECTIVE_OUTCOME_ADV_INVALID"
                    )
                result[symbol] = float(adv)
        return result

    def _baseline_current(
        self,
        request: ProspectiveRequestEvidenceV1,
    ) -> dict[str, float]:
        if request.prior_prospective_request_id is None:
            return {
                item.symbol: 0.0 for item in request.request.instruments
            }
        prior = self._prospective.request(
            request.prior_prospective_request_id
        )
        if prior is None:
            raise ProspectiveOutcomeError(
                "PROSPECTIVE_OUTCOME_PRIOR_REQUEST_INVALID"
            )
        return self._parent_targets(prior)

    @staticmethod
    def _parent_targets(
        request: ProspectiveRequestEvidenceV1,
    ) -> dict[str, float]:
        targets: dict[str, float] = {}
        for instrument in request.request.instruments:
            feature = next(
                (
                    item
                    for item in instrument.features
                    if item.name == "parent_target_weight"
                ),
                None,
            )
            if feature is None or not 0 <= feature.value <= 1:
                raise ProspectiveOutcomeError(
                    "PROSPECTIVE_OUTCOME_PARENT_TARGET_INVALID"
                )
            targets[instrument.symbol] = feature.value
        if sum(targets.values()) > 1 + request.request.constraints.numeric_tolerance:
            raise ProspectiveOutcomeError(
                "PROSPECTIVE_OUTCOME_PARENT_TARGET_LEVERAGED"
            )
        return targets

    def _evaluation_nav(
        self,
        request: ProspectiveRequestEvidenceV1,
    ) -> float:
        with self._session_factory() as session:
            row = session.get(
                StrategyEvaluationAnchorRow,
                request.evaluation_anchor_id,
            )
        if row is None or row.initial_nav_usd <= 0:
            raise ProspectiveOutcomeError(
                "PROSPECTIVE_OUTCOME_EVALUATION_ANCHOR_INVALID"
            )
        return float(row.initial_nav_usd)

    def _regime(
        self,
        market_return: float,
    ) -> Literal["UP", "DOWN", "RANGE"]:
        context = self._config.config.market_context
        if market_return >= context.up_regime_return_threshold:
            return "UP"
        if market_return <= context.down_regime_return_threshold:
            return "DOWN"
        return "RANGE"


def prospective_outcome_status(
    repository: ProspectiveOutcomeRepository,
    *,
    config: ProspectiveOutcomeConfigBundle,
    challenger_id: str | None,
) -> dict[str, object]:
    if challenger_id is None:
        status: dict[str, object] = {
            "schema_version": "candidate_prospective_outcome_status_v1",
            "challenger_id": None,
            "status": "WAITING_FOR_PROSPECTIVE_TARGET",
            "outcome_count": 0,
            "terminal_failure_count": 0,
            "observation_count": 0,
            "minimum_common_sessions": (
                config.config.readiness.minimum_common_sessions
            ),
            "minimum_observations": (
                config.config.readiness.minimum_observations
            ),
            "falsification_input_ready": False,
            "latest": None,
            "latest_terminal_failure": None,
            "challenger_status_advanced": False,
            "falsification_started": False,
            "oos_started": False,
            "shadow_started": False,
            "automatic_promotion_enabled": False,
            "broker_access_permitted": False,
            "real_order_routing": False,
        }
    else:
        status = repository.status(
            challenger_id=challenger_id,
            minimum_common_sessions=(
                config.config.readiness.minimum_common_sessions
            ),
            minimum_observations=(
                config.config.readiness.minimum_observations
            ),
        )
        status["status"] = (
            "FALSIFICATION_INPUT_READY"
            if status["falsification_input_ready"]
            else "ACCUMULATING_FORWARD_OUTCOMES"
        )
    return {
        **status,
        "producer_version": config.config.producer_version,
        "price_rule": config.config.implementation.price_rule,
        "return_horizon_sessions": (
            config.config.implementation.return_horizon_sessions
        ),
        "calendar_version": config.config.calendar_version,
        "market_dataset_version": config.config.market_dataset_version,
        "config_manifest_hash": config.manifest_hash,
        "cost_model_hash": config.cost_model_hash,
        "challenger_lifecycle_advance_enabled": False,
        "shadow_activation_enabled": False,
        "automatic_promotion_enabled": False,
        "broker_access_permitted": False,
        "real_order_routing": False,
    }


def _source_bar(row: MarketBarRow) -> ProspectiveOutcomeSourceBarV1:
    return ProspectiveOutcomeSourceBarV1(
        bar_id=row.bar_id,
        symbol=row.symbol,
        session_date=_session_date(row),
        source_event_time=_utc(row.event_time),
        available_at=_utc(row.available_at),
        adjusted_close=float(row.close),
        volume=float(row.volume),
        payload_hash=row.payload_hash,
    )


def _simple_return(start: MarketBarRow, end: MarketBarRow) -> float:
    value = float(end.close / start.close - 1)
    if not math.isfinite(value) or value <= -1:
        raise ProspectiveOutcomeError(
            "PROSPECTIVE_OUTCOME_RETURN_INVALID"
        )
    return value


def _session_date(row: MarketBarRow) -> date:
    return _utc(row.event_time).astimezone(NEW_YORK).date()


def _utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=UTC)
        if value.tzinfo is None
        else value.astimezone(UTC)
    )
