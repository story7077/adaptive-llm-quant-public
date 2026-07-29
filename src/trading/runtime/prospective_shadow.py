from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import desc, exists, func, select
from sqlalchemy.orm import Session, sessionmaker

from trading.domain.hashing import stable_id
from trading.domain.q1 import Q1_ALGORITHM_VERSION, Q1ArmId, Q1StrategyDecision
from trading.domain.time import require_aware_utc
from trading.persistence.models import (
    AlgorithmProposalRow,
    ChallengerManifestRow,
    DomainEventRow,
    MarketBarRow,
    MarketQuoteRow,
    MarketStreamStatusRow,
    PortfolioDecisionRow,
    ResearchCandidateArtifactRow,
    ResearchCandidateProspectiveExecutionRow,
    ResearchCandidateProspectiveRequestRow,
    RunRow,
)
from trading.persistence.prospective import ProspectiveCandidateRepository
from trading.persistence.research_shadow import (
    ResearchShadowRuntimeRepository,
)
from trading.research.candidate_abi import CandidateDecisionResponseV1
from trading.research.prospective import (
    ProspectiveCandidateConfigBundle,
    ProspectiveExecutionStatus,
    ProspectiveRequestEvidenceV1,
    ProspectiveSourceBarV1,
)
from trading.research.prospective_shadow import (
    PROSPECTIVE_SHADOW_SOURCE_AGGREGATE,
    ProspectiveShadowCycleSourceV1,
    build_prospective_shadow_cycle_source,
)
from trading.research.shadow_runtime import (
    SHADOW_RUNTIME_VERSION,
    MatchedQuoteBundleV1,
    MatchedShadowCycleResultV1,
    ShadowArmRole,
    ShadowPairRuntimeSpecV1,
    ShadowQuoteV1,
    ShadowTargetDecisionV1,
    build_matched_quote_bundle,
    build_shadow_target_decision,
)


class ProspectiveShadowOperationError(RuntimeError):
    """Fail-closed trusted prospective-to-shadow bridge error."""


PROSPECTIVE_SHADOW_WAIT_ERROR_CODES = frozenset(
    {
        "PROSPECTIVE_SHADOW_DECISION_WINDOW_CLOSED",
        "PROSPECTIVE_SHADOW_FRESH_MATCHED_QUOTES_NOT_AVAILABLE",
        "PROSPECTIVE_SHADOW_MARKET_STREAM_NOT_CONNECTED",
        "PROSPECTIVE_SHADOW_QUOTE_SKEW_EXCEEDED",
    }
)


@dataclass(frozen=True, slots=True)
class PreparedProspectiveShadowCycle:
    provenance: ProspectiveShadowCycleSourceV1
    champion_target: ShadowTargetDecisionV1
    challenger_target: ShadowTargetDecisionV1
    quote_bundle: MatchedQuoteBundleV1


@dataclass(frozen=True, slots=True)
class CommittedProspectiveShadowCycle:
    prepared: PreparedProspectiveShadowCycle
    cycle: MatchedShadowCycleResultV1
    replay_hash: str


class TrustedProspectiveShadowOperations:
    """Derive shadow inputs only from immutable host-side evidence."""

    real_order_routing = False
    automatic_promotion_enabled = False

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        prospective_config: ProspectiveCandidateConfigBundle,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._config = prospective_config
        self._clock = clock
        self._prospective = ProspectiveCandidateRepository(session_factory)
        self._shadow = ResearchShadowRuntimeRepository(session_factory)

    def active_run_id(self, challenger_id: str) -> str | None:
        with self._session_factory() as session:
            rows = tuple(
                session.scalars(
                    select(RunRow)
                    .where(
                        RunRow.experiment_version
                        == SHADOW_RUNTIME_VERSION,
                        RunRow.status == "RUNNING",
                    )
                    .order_by(desc(RunRow.started_at), desc(RunRow.run_id))
                )
            )
        for row in rows:
            spec = self._shadow.load_spec(row.run_id)
            if spec.challenger_id == challenger_id:
                return spec.run_id
        return None

    def next_eligible_request_id(
        self,
        *,
        run_id: str,
        as_of: datetime | None = None,
    ) -> str | None:
        spec = self._shadow.load_spec(run_id)
        instant = self._database_clock() if as_of is None else require_aware_utc(as_of)
        already_committed = exists(
            select(DomainEventRow.event_id).where(
                DomainEventRow.aggregate_type
                == PROSPECTIVE_SHADOW_SOURCE_AGGREGATE,
                DomainEventRow.aggregate_id == run_id,
                DomainEventRow.correlation_id
                == ResearchCandidateProspectiveRequestRow.prospective_request_id,
            )
        )
        statement = (
            select(ResearchCandidateProspectiveRequestRow.prospective_request_id)
            .join(
                ResearchCandidateProspectiveExecutionRow,
                ResearchCandidateProspectiveExecutionRow.prospective_request_id
                == ResearchCandidateProspectiveRequestRow.prospective_request_id,
            )
            .join(
                PortfolioDecisionRow,
                PortfolioDecisionRow.portfolio_decision_id
                == ResearchCandidateProspectiveRequestRow.parent_portfolio_decision_id,
            )
            .where(
                ResearchCandidateProspectiveRequestRow.challenger_id
                == spec.challenger_id,
                ResearchCandidateProspectiveRequestRow.recorded_at
                >= spec.created_at,
                ResearchCandidateProspectiveExecutionRow.recorded_at
                >= spec.created_at,
                ResearchCandidateProspectiveExecutionRow.status
                == ProspectiveExecutionStatus.SUCCEEDED,
                PortfolioDecisionRow.valid_until > instant,
                ~already_committed,
            )
            .order_by(
                ResearchCandidateProspectiveRequestRow.parent_scheduled_at,
                ResearchCandidateProspectiveRequestRow.prospective_request_id,
            )
            .limit(1)
        )
        with self._session_factory() as session:
            return session.scalar(statement)

    def prepare(
        self,
        *,
        run_id: str,
        prospective_request_id: str,
        as_of: datetime | None = None,
    ) -> PreparedProspectiveShadowCycle:
        now = self._database_clock()
        quote_as_of = now if as_of is None else require_aware_utc(as_of)
        spec = self._shadow.load_spec(run_id)
        maximum_age = spec.paper_parameters.maximum_quote_age_seconds
        if quote_as_of > now or now - quote_as_of > timedelta(seconds=maximum_age):
            raise ProspectiveShadowOperationError(
                "PROSPECTIVE_SHADOW_AS_OF_NOT_CURRENT"
            )
        with self._session_factory() as session:
            request_row = session.get(
                ResearchCandidateProspectiveRequestRow,
                prospective_request_id,
            )
            execution_row = session.scalar(
                select(ResearchCandidateProspectiveExecutionRow).where(
                    ResearchCandidateProspectiveExecutionRow.success_identity
                    == prospective_request_id
                )
            )
            if request_row is None or execution_row is None:
                raise ProspectiveShadowOperationError(
                    "PROSPECTIVE_SHADOW_EVIDENCE_NOT_AVAILABLE"
                )
            parent_row = session.get(
                PortfolioDecisionRow,
                request_row.parent_portfolio_decision_id,
            )
            artifact_row = session.get(
                ResearchCandidateArtifactRow,
                request_row.candidate_artifact_bundle_id,
            )
            manifest_row = session.get(
                ChallengerManifestRow,
                spec.challenger_id,
            )
            proposal_row = (
                None
                if manifest_row is None
                else session.get(AlgorithmProposalRow, manifest_row.proposal_id)
            )
            request_recorded_at = _aware(request_row.recorded_at)
            execution_recorded_at = _aware(execution_row.recorded_at)
        if (
            parent_row is None
            or artifact_row is None
            or manifest_row is None
            or proposal_row is None
        ):
            raise ProspectiveShadowOperationError(
                "PROSPECTIVE_SHADOW_DATABASE_BINDING_INCOMPLETE"
            )
        try:
            request = ProspectiveRequestEvidenceV1.model_validate(
                request_row.payload_json
            )
            execution = self._prospective.successful_execution(
                prospective_request_id
            )
            parent = Q1StrategyDecision.model_validate(parent_row.payload_json)
        except ValueError as exc:
            raise ProspectiveShadowOperationError(
                "PROSPECTIVE_SHADOW_STORED_EVIDENCE_INVALID"
            ) from exc
        if execution is None:
            raise ProspectiveShadowOperationError(
                "PROSPECTIVE_SHADOW_SUCCESS_NOT_AVAILABLE"
            )
        primary = execution.primary_response
        replay = execution.replay_response
        if primary is None or replay is None:
            raise ProspectiveShadowOperationError(
                "PROSPECTIVE_SHADOW_SUCCESS_OUTPUT_MISSING"
            )
        primary.assert_bound_to(request.request)
        replay.assert_bound_to(request.request)
        self._validate_bindings(
            spec=spec,
            request=request,
            request_row=request_row,
            execution_row=execution_row,
            execution_hash=execution.execution_hash,
            primary_hash=primary.output_hash,
            replay_hash=replay.output_hash,
            parent=parent,
            parent_row=parent_row,
            artifact_row=artifact_row,
            manifest_row=manifest_row,
            proposal_row=proposal_row,
            request_recorded_at=request_recorded_at,
            execution_recorded_at=execution_recorded_at,
            prospective_config=self._config,
        )
        decision_available_at = max(
            request_recorded_at,
            execution_recorded_at,
            parent.decision_created_at,
        )
        if (
            decision_available_at >= parent.valid_until
            or quote_as_of <= decision_available_at
            or quote_as_of >= parent.valid_until
        ):
            raise ProspectiveShadowOperationError(
                "PROSPECTIVE_SHADOW_DECISION_WINDOW_CLOSED"
            )
        champion_weights = _champion_weights(parent)
        challenger_weights = _challenger_weights(primary)
        champion_state, challenger_state = self._shadow.latest_states(run_id)
        required_symbols = (
            (set(champion_weights) | set(challenger_weights))
            - {"USD_CASH"}
        ) | set(champion_state.position_map()) | set(challenger_state.position_map())
        request_symbols = tuple(item.symbol for item in request.request.instruments)
        if not required_symbols:
            required_symbols.add(request_symbols[0])
        if not required_symbols.issubset(set(request_symbols)):
            raise ProspectiveShadowOperationError(
                "PROSPECTIVE_SHADOW_TARGET_OUTSIDE_REQUEST_UNIVERSE"
            )
        quote_bundle, quote_source_hashes, adv_bar_ids = self._market_bundle(
            request=request,
            symbols=tuple(sorted(required_symbols)),
            decision_available_at=decision_available_at,
            quote_as_of=quote_as_of,
            market_input_manifest_hash=spec.market_input_manifest_hash,
            spec=spec,
        )
        champion_target = build_shadow_target_decision(
            target_id=_target_id(
                run_id,
                prospective_request_id,
                ShadowArmRole.CHAMPION,
            ),
            spec=spec,
            role=ShadowArmRole.CHAMPION,
            decision_time=decision_available_at,
            signal_data_cutoff=request.request.signal_data_cutoff,
            valid_until=parent.valid_until,
            quote_manifest_hash=quote_bundle.quote_manifest_hash,
            target_weights=champion_weights,
        )
        challenger_target = build_shadow_target_decision(
            target_id=_target_id(
                run_id,
                prospective_request_id,
                ShadowArmRole.CHALLENGER,
            ),
            spec=spec,
            role=ShadowArmRole.CHALLENGER,
            decision_time=decision_available_at,
            signal_data_cutoff=request.request.signal_data_cutoff,
            valid_until=parent.valid_until,
            quote_manifest_hash=quote_bundle.quote_manifest_hash,
            target_weights=challenger_weights,
        )
        provenance = build_prospective_shadow_cycle_source(
            run_id=run_id,
            shadow_pair_id=spec.shadow_pair_id,
            challenger_id=spec.challenger_id,
            prospective_request_id=prospective_request_id,
            request_evidence_hash=request.evidence_hash,
            request_recorded_at=request_recorded_at,
            prospective_execution_id=execution.execution_id,
            prospective_execution_hash=execution.execution_hash,
            execution_recorded_at=execution_recorded_at,
            runtime_attestation_hash=execution.runtime_attestation_hash,
            security_contract_hash=execution.security_contract_hash,
            primary_response_hash=primary.output_hash,
            replay_response_hash=replay.output_hash,
            parent_run_id=parent.run_id,
            parent_portfolio_decision_id=parent.portfolio_decision_id,
            parent_decision_hash=parent.decision_hash,
            parent_input_manifest_hash=parent.input_manifest.manifest_hash,
            parent_signal_data_cutoff=parent.signal_data_cutoff,
            candidate_signal_data_cutoff=request.request.signal_data_cutoff,
            candidate_artifact_hash=request.candidate_artifact_hash,
            prospective_source_manifest_hash=request.source_manifest.manifest_hash,
            champion_target=champion_target,
            challenger_target=challenger_target,
            quote_bundle=quote_bundle,
            quote_source_hash_by_symbol=quote_source_hashes,
            adv_source_bar_ids_by_symbol=adv_bar_ids,
            decision_available_at=decision_available_at,
            recorded_at=now,
        )
        return PreparedProspectiveShadowCycle(
            provenance=provenance,
            champion_target=champion_target,
            challenger_target=challenger_target,
            quote_bundle=quote_bundle,
        )

    def preview(
        self,
        *,
        run_id: str,
        prospective_request_id: str,
        as_of: datetime | None = None,
    ) -> tuple[PreparedProspectiveShadowCycle, MatchedShadowCycleResultV1]:
        prepared = self.prepare(
            run_id=run_id,
            prospective_request_id=prospective_request_id,
            as_of=as_of,
        )
        cycle = self._shadow.preview_matched_cycle(
            run_id=run_id,
            champion_target=prepared.champion_target,
            challenger_target=prepared.challenger_target,
            quote_bundle=prepared.quote_bundle,
        )
        return prepared, cycle

    def commit(
        self,
        *,
        run_id: str,
        prospective_request_id: str,
        as_of: datetime | None = None,
    ) -> CommittedProspectiveShadowCycle:
        prepared = self.prepare(
            run_id=run_id,
            prospective_request_id=prospective_request_id,
            as_of=as_of,
        )
        cycle = self._shadow.append_prospective_matched_cycle(
            run_id=run_id,
            champion_target=prepared.champion_target,
            challenger_target=prepared.challenger_target,
            quote_bundle=prepared.quote_bundle,
            provenance=prepared.provenance,
        )
        return CommittedProspectiveShadowCycle(
            prepared=prepared,
            cycle=cycle,
            replay_hash=self._shadow.deterministic_replay_hash(run_id),
        )

    def commit_next_for_challenger(
        self,
        *,
        challenger_id: str,
        as_of: datetime | None = None,
    ) -> CommittedProspectiveShadowCycle | None:
        run_id = self.active_run_id(challenger_id)
        if run_id is None:
            return None
        request_id = self.next_eligible_request_id(run_id=run_id, as_of=as_of)
        if request_id is None:
            return None
        return self.commit(
            run_id=run_id,
            prospective_request_id=request_id,
            as_of=as_of,
        )

    def _market_bundle(
        self,
        *,
        request: ProspectiveRequestEvidenceV1,
        symbols: tuple[str, ...],
        decision_available_at: datetime,
        quote_as_of: datetime,
        market_input_manifest_hash: str,
        spec: ShadowPairRuntimeSpecV1,
    ) -> tuple[
        MatchedQuoteBundleV1,
        dict[str, str],
        dict[str, tuple[str, ...]],
    ]:
        parameters = spec.paper_parameters
        market = self._config.config.market_data
        with self._session_factory() as session:
            status = session.get(
                MarketStreamStatusRow,
                (market.provider, market.feed),
            )
            if status is None or status.state != "CONNECTED":
                raise ProspectiveShadowOperationError(
                    "PROSPECTIVE_SHADOW_MARKET_STREAM_NOT_CONNECTED"
                )
            quote_rows: dict[str, MarketQuoteRow] = {}
            oldest = quote_as_of - timedelta(
                seconds=parameters.maximum_quote_age_seconds
            )
            for symbol in symbols:
                row = session.scalar(
                    select(MarketQuoteRow)
                    .where(
                        MarketQuoteRow.provider == market.provider,
                        MarketQuoteRow.feed == market.feed,
                        MarketQuoteRow.symbol == symbol,
                        MarketQuoteRow.event_time > decision_available_at,
                        MarketQuoteRow.event_time >= oldest,
                        MarketQuoteRow.event_time <= quote_as_of,
                        MarketQuoteRow.available_at > decision_available_at,
                        MarketQuoteRow.available_at <= quote_as_of,
                        MarketQuoteRow.bid_price > 0,
                        MarketQuoteRow.ask_price > 0,
                        MarketQuoteRow.ask_price >= MarketQuoteRow.bid_price,
                        MarketQuoteRow.bid_size_round_lots > 0,
                        MarketQuoteRow.ask_size_round_lots > 0,
                    )
                    .order_by(
                        desc(MarketQuoteRow.available_at),
                        desc(MarketQuoteRow.event_time),
                        desc(MarketQuoteRow.quote_id),
                    )
                    .limit(1)
                )
                if row is None:
                    raise ProspectiveShadowOperationError(
                        "PROSPECTIVE_SHADOW_FRESH_MATCHED_QUOTES_NOT_AVAILABLE"
                    )
                quote_rows[symbol] = row
            event_times = tuple(_aware(row.event_time) for row in quote_rows.values())
            if (
                max(event_times) - min(event_times)
            ).total_seconds() > parameters.maximum_multi_symbol_quote_skew_seconds:
                raise ProspectiveShadowOperationError(
                    "PROSPECTIVE_SHADOW_QUOTE_SKEW_EXCEEDED"
                )
            adv_values, adv_ids = self._adv_sources(
                session,
                request=request,
                symbols=symbols,
                lookback=parameters.adv_lookback_completed_sessions,
            )
        quotes = tuple(
            ShadowQuoteV1(
                quote_id=row.quote_id,
                instrument_id=symbol,
                event_time=_aware(row.event_time),
                available_at=_aware(row.available_at),
                bid_price=row.bid_price,
                ask_price=row.ask_price,
                bid_size_shares=(
                    Decimal(row.bid_size_round_lots)
                    * Decimal(parameters.displayed_size_unit_shares)
                ),
                ask_size_shares=(
                    Decimal(row.ask_size_round_lots)
                    * Decimal(parameters.displayed_size_unit_shares)
                ),
                adv_shares=adv_values[symbol],
                source_hash=row.payload_hash,
            )
            for symbol, row in sorted(quote_rows.items())
        )
        return (
            build_matched_quote_bundle(
                market_input_manifest_hash=market_input_manifest_hash,
                as_of=quote_as_of,
                quotes=quotes,
            ),
            {
                symbol: row.payload_hash
                for symbol, row in sorted(quote_rows.items())
            },
            adv_ids,
        )

    def _adv_sources(
        self,
        session: Session,
        *,
        request: ProspectiveRequestEvidenceV1,
        symbols: tuple[str, ...],
        lookback: int,
    ) -> tuple[dict[str, Decimal], dict[str, tuple[str, ...]]]:
        by_symbol: dict[str, list[ProspectiveSourceBarV1]] = {
            symbol: [] for symbol in symbols
        }
        for source in request.source_manifest.source_bars:
            if source.symbol in by_symbol:
                by_symbol[source.symbol].append(source)
        values: dict[str, Decimal] = {}
        ids: dict[str, tuple[str, ...]] = {}
        market = self._config.config.market_data
        for symbol, sources in sorted(by_symbol.items()):
            session_dates = tuple(item.session_date for item in sources)
            if (
                len(session_dates) != len(set(session_dates))
                or any(
                    session_date >= request.request.decision_time.date()
                    for session_date in session_dates
                )
            ):
                raise ProspectiveShadowOperationError(
                    "PROSPECTIVE_SHADOW_ADV_SESSION_SET_INVALID"
                )
            ordered = sorted(
                sources,
                key=lambda item: (item.session_date, item.bar_id),
            )
            selected = ordered[-lookback:]
            if len(selected) != lookback:
                raise ProspectiveShadowOperationError(
                    "PROSPECTIVE_SHADOW_ADV_HISTORY_INCOMPLETE"
                )
            rows = {
                row.bar_id: row
                for row in session.scalars(
                    select(MarketBarRow).where(
                        MarketBarRow.bar_id.in_(
                            tuple(item.bar_id for item in selected)
                        )
                    )
                )
            }
            volumes: list[Decimal] = []
            for source in selected:
                row = rows.get(source.bar_id)
                if (
                    row is None
                    or row.symbol != symbol
                    or row.provider != market.provider
                    or row.feed != market.feed
                    or row.timeframe != market.timeframe
                    or row.payload_hash != source.payload_hash
                    or _aware(row.event_time) != source.source_event_time
                    or _aware(row.available_at) != source.available_at
                    or _aware(row.available_at) > request.request.signal_data_cutoff
                    or row.payload_json.get("_adjustment") != market.adjustment
                    or row.payload_json.get("_dataset_version")
                    != market.dataset_version
                    or row.volume <= 0
                ):
                    raise ProspectiveShadowOperationError(
                        "PROSPECTIVE_SHADOW_ADV_SOURCE_BINDING_INVALID"
                    )
                volumes.append(row.volume)
            average = sum(volumes, Decimal("0")) / Decimal(lookback)
            if average <= 0:
                raise ProspectiveShadowOperationError(
                    "PROSPECTIVE_SHADOW_ADV_NON_POSITIVE"
                )
            values[symbol] = average
            ids[symbol] = tuple(sorted(item.bar_id for item in selected))
        return values, ids

    @staticmethod
    def _validate_bindings(
        *,
        spec: ShadowPairRuntimeSpecV1,
        request: ProspectiveRequestEvidenceV1,
        request_row: ResearchCandidateProspectiveRequestRow,
        execution_row: ResearchCandidateProspectiveExecutionRow,
        execution_hash: str,
        primary_hash: str,
        replay_hash: str,
        parent: Q1StrategyDecision,
        parent_row: PortfolioDecisionRow,
        artifact_row: ResearchCandidateArtifactRow,
        manifest_row: ChallengerManifestRow,
        proposal_row: AlgorithmProposalRow,
        request_recorded_at: datetime,
        execution_recorded_at: datetime,
        prospective_config: ProspectiveCandidateConfigBundle,
    ) -> None:
        configured = prospective_config.config
        if (
            request.challenger_id != spec.challenger_id
            or request.candidate_artifact_hash != spec.challenger.artifact_hash
            or request.request.strategy_id != spec.challenger.strategy_id
            or request.request.strategy_version != spec.challenger.strategy_version
            or request.request.strategy_id != configured.strategy_id
            or request.request.strategy_version != configured.strategy_version
            or tuple(item.symbol for item in request.request.instruments)
            != configured.reference_universe
            or request.source_manifest.host_config_manifest_hash
            != prospective_config.manifest_hash
            or request.source_manifest.market_dataset_version
            != configured.market_data.dataset_version
            or request.strategy_config_content_sha256
            != configured.strategy_config_content_sha256
            or request.evidence_hash != request_row.evidence_hash
            or request.request.request_hash != request_row.request_hash
            or execution_row.execution_hash != execution_hash
            or execution_row.primary_response_hash != primary_hash
            or execution_row.replay_response_hash != replay_hash
            or not execution_row.deterministic_match
            or primary_hash != replay_hash
            or parent.portfolio_decision_id
            != request.parent_portfolio_decision_id
            or parent.run_id != request.parent_run_id
            or parent.arm_id is not Q1ArmId.Q1_DET
            or parent.algorithm_version != Q1_ALGORITHM_VERSION
            or parent.decision_kind != "STRATEGIC_TARGET"
            or parent.decision_hash != parent_row.decision_hash
            or parent.input_manifest.manifest_hash
            != request.source_manifest.parent_input_manifest_hash
            or parent.decision_hash
            != request.source_manifest.parent_decision_hash
            or artifact_row.bundle_hash != request.candidate_artifact_hash
            or artifact_row.challenger_id != spec.challenger_id
            or artifact_row.config_hash != request.candidate_config_hash
            or artifact_row.real_order_routing
            or manifest_row.challenger_id != spec.challenger_id
            or manifest_row.strategy_id != spec.challenger.strategy_id
            or manifest_row.strategy_version != spec.challenger.strategy_version
            or manifest_row.config_hash != request.candidate_config_hash
            or proposal_row.parent_strategy_id != spec.champion.strategy_id
            or proposal_row.parent_strategy_version
            != spec.champion.strategy_version
            or request_recorded_at < spec.created_at
            or execution_recorded_at < spec.created_at
            or parent.decision_created_at < spec.created_at
        ):
            raise ProspectiveShadowOperationError(
                "PROSPECTIVE_SHADOW_SOURCE_BINDING_MISMATCH"
            )

    def _database_clock(self) -> datetime:
        if self._clock is not None:
            return require_aware_utc(self._clock())
        with self._session_factory() as session:
            value = session.scalar(select(func.now()))
        if not isinstance(value, datetime):
            raise ProspectiveShadowOperationError(
                "PROSPECTIVE_SHADOW_DATABASE_CLOCK_UNAVAILABLE"
            )
        return _aware(value)


def _champion_weights(parent: Q1StrategyDecision) -> dict[str, Decimal]:
    weights = {
        symbol: value
        for symbol, value in parent.target_weights.items()
        if value > 0 or symbol == "USD_CASH"
    }
    if "USD_CASH" not in weights:
        weights["USD_CASH"] = Decimal("1") - sum(
            weights.values(),
            Decimal("0"),
        )
    return dict(sorted(weights.items()))


def _challenger_weights(
    response: CandidateDecisionResponseV1,
) -> dict[str, Decimal]:
    targets = response.targets
    risky = {
        item.symbol: Decimal(str(item.target_weight))
        for item in targets
        if item.target_weight > 0
    }
    cash = Decimal("1") - sum(risky.values(), Decimal("0"))
    if cash < 0:
        raise ProspectiveShadowOperationError(
            "PROSPECTIVE_SHADOW_CANDIDATE_CASH_NEGATIVE"
        )
    return dict(sorted({**risky, "USD_CASH": cash}.items()))


def _target_id(
    run_id: str,
    prospective_request_id: str,
    role: ShadowArmRole,
) -> str:
    return stable_id(
        "prospective-shadow-target",
        run_id,
        prospective_request_id,
        role.value,
    )


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


__all__ = [
    "PROSPECTIVE_SHADOW_WAIT_ERROR_CODES",
    "CommittedProspectiveShadowCycle",
    "PreparedProspectiveShadowCycle",
    "ProspectiveShadowOperationError",
    "TrustedProspectiveShadowOperations",
]
