from __future__ import annotations

import re
from datetime import datetime
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from trading.domain.contracts import DomainModel
from trading.domain.hashing import canonical_hash, stable_id
from trading.domain.time import require_aware_utc
from trading.research.contracts import (
    HASH_PATTERN,
    IDENTIFIER_PATTERN,
    SYMBOL_PATTERN,
)
from trading.research.shadow_runtime import (
    MatchedQuoteBundleV1,
    ShadowTargetDecisionV1,
)

PROSPECTIVE_SHADOW_SOURCE_AGGREGATE = "RESEARCH_PROSPECTIVE_SHADOW_SOURCE"
TRUSTED_SHADOW_CYCLE_EVENT = "TRUSTED_PROSPECTIVE_MATCHED_PAPER_CYCLE_COMMITTED"
UNATTESTED_SHADOW_CYCLE_EVENT = "UNATTESTED_MATCHED_PAPER_CYCLE_COMMITTED"


class ProspectiveShadowCycleSourceV1(DomainModel):
    """Host-attested sources for one promotion-eligible shadow cycle."""

    schema_version: Literal["prospective_shadow_cycle_source_v1"] = (
        "prospective_shadow_cycle_source_v1"
    )
    provenance_id: str = Field(pattern=IDENTIFIER_PATTERN)
    run_id: str = Field(pattern=IDENTIFIER_PATTERN)
    shadow_pair_id: str = Field(pattern=IDENTIFIER_PATTERN)
    challenger_id: str = Field(pattern=IDENTIFIER_PATTERN)
    prospective_request_id: str = Field(pattern=IDENTIFIER_PATTERN)
    request_evidence_hash: str = Field(pattern=HASH_PATTERN)
    request_recorded_at: datetime
    prospective_execution_id: str = Field(pattern=IDENTIFIER_PATTERN)
    prospective_execution_hash: str = Field(pattern=HASH_PATTERN)
    execution_recorded_at: datetime
    runtime_attestation_hash: str = Field(pattern=HASH_PATTERN)
    security_contract_hash: str = Field(pattern=HASH_PATTERN)
    primary_response_hash: str = Field(pattern=HASH_PATTERN)
    replay_response_hash: str = Field(pattern=HASH_PATTERN)
    parent_run_id: str = Field(pattern=IDENTIFIER_PATTERN)
    parent_portfolio_decision_id: str = Field(pattern=IDENTIFIER_PATTERN)
    parent_decision_hash: str = Field(pattern=HASH_PATTERN)
    parent_input_manifest_hash: str = Field(pattern=HASH_PATTERN)
    parent_signal_data_cutoff: datetime
    candidate_signal_data_cutoff: datetime
    candidate_artifact_hash: str = Field(pattern=HASH_PATTERN)
    prospective_source_manifest_hash: str = Field(pattern=HASH_PATTERN)
    champion_target_hash: str = Field(pattern=HASH_PATTERN)
    challenger_target_hash: str = Field(pattern=HASH_PATTERN)
    quote_bundle_hash: str = Field(pattern=HASH_PATTERN)
    quote_manifest_hash: str = Field(pattern=HASH_PATTERN)
    quote_id_by_symbol: dict[str, str]
    quote_source_hash_by_symbol: dict[str, str]
    adv_source_bar_ids_by_symbol: dict[str, tuple[str, ...]]
    decision_available_at: datetime
    quote_as_of: datetime
    recorded_at: datetime
    deterministic_primary_replay_match: Literal[True] = True
    market_stream_connected: Literal[True] = True
    trusted_for_promotion_evidence: Literal[True] = True
    automatic_promotion_enabled: Literal[False] = False
    real_order_routing: Literal[False] = False
    provenance_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator(
        "request_recorded_at",
        "execution_recorded_at",
        "parent_signal_data_cutoff",
        "candidate_signal_data_cutoff",
        "decision_available_at",
        "quote_as_of",
        "recorded_at",
        mode="after",
    )
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_provenance(self) -> Self:
        symbol_sets = (
            set(self.quote_id_by_symbol),
            set(self.quote_source_hash_by_symbol),
            set(self.adv_source_bar_ids_by_symbol),
        )
        if not symbol_sets[0] or any(
            value != symbol_sets[0] for value in symbol_sets[1:]
        ):
            raise ValueError("prospective shadow quote and ADV symbols differ")
        if any(
            not _valid_symbol(symbol)
            or re.fullmatch(IDENTIFIER_PATTERN, quote_id) is None
            or re.fullmatch(HASH_PATTERN, source_hash) is None
            or not bar_ids
            or len(bar_ids) != len(set(bar_ids))
            or any(
                re.fullmatch(IDENTIFIER_PATTERN, bar_id) is None
                for bar_id in bar_ids
            )
            for symbol, quote_id, source_hash, bar_ids in (
                (
                    symbol,
                    self.quote_id_by_symbol[symbol],
                    self.quote_source_hash_by_symbol[symbol],
                    self.adv_source_bar_ids_by_symbol[symbol],
                )
                for symbol in sorted(symbol_sets[0])
            )
        ):
            raise ValueError("prospective shadow market provenance is invalid")
        if any(
            value != tuple(sorted(value))
            for value in self.adv_source_bar_ids_by_symbol.values()
        ):
            raise ValueError("prospective shadow ADV source IDs must be sorted")
        if len(set(self.quote_id_by_symbol.values())) != len(self.quote_id_by_symbol):
            raise ValueError("prospective shadow quote IDs must be unique")
        if self.primary_response_hash != self.replay_response_hash:
            raise ValueError("prospective shadow source is nondeterministic")
        if (
            self.decision_available_at
            < max(self.request_recorded_at, self.execution_recorded_at)
            or self.quote_as_of <= self.decision_available_at
            or self.recorded_at < self.quote_as_of
        ):
            raise ValueError("prospective shadow source times are invalid")
        payload = self.model_dump(mode="python", exclude={"provenance_hash"})
        if canonical_hash(payload) != self.provenance_hash:
            raise ValueError("prospective shadow provenance hash mismatch")
        return self


def build_prospective_shadow_cycle_source(
    *,
    run_id: str,
    shadow_pair_id: str,
    challenger_id: str,
    prospective_request_id: str,
    request_evidence_hash: str,
    request_recorded_at: datetime,
    prospective_execution_id: str,
    prospective_execution_hash: str,
    execution_recorded_at: datetime,
    runtime_attestation_hash: str,
    security_contract_hash: str,
    primary_response_hash: str,
    replay_response_hash: str,
    parent_run_id: str,
    parent_portfolio_decision_id: str,
    parent_decision_hash: str,
    parent_input_manifest_hash: str,
    parent_signal_data_cutoff: datetime,
    candidate_signal_data_cutoff: datetime,
    candidate_artifact_hash: str,
    prospective_source_manifest_hash: str,
    champion_target: ShadowTargetDecisionV1,
    challenger_target: ShadowTargetDecisionV1,
    quote_bundle: MatchedQuoteBundleV1,
    quote_source_hash_by_symbol: dict[str, str],
    adv_source_bar_ids_by_symbol: dict[str, tuple[str, ...]],
    decision_available_at: datetime,
    recorded_at: datetime,
) -> ProspectiveShadowCycleSourceV1:
    provenance_id = stable_id(
        "prospective-shadow-cycle-source",
        run_id,
        prospective_request_id,
        prospective_execution_hash,
    )
    payload = {
        "schema_version": "prospective_shadow_cycle_source_v1",
        "provenance_id": provenance_id,
        "run_id": run_id,
        "shadow_pair_id": shadow_pair_id,
        "challenger_id": challenger_id,
        "prospective_request_id": prospective_request_id,
        "request_evidence_hash": request_evidence_hash,
        "request_recorded_at": require_aware_utc(request_recorded_at),
        "prospective_execution_id": prospective_execution_id,
        "prospective_execution_hash": prospective_execution_hash,
        "execution_recorded_at": require_aware_utc(execution_recorded_at),
        "runtime_attestation_hash": runtime_attestation_hash,
        "security_contract_hash": security_contract_hash,
        "primary_response_hash": primary_response_hash,
        "replay_response_hash": replay_response_hash,
        "parent_run_id": parent_run_id,
        "parent_portfolio_decision_id": parent_portfolio_decision_id,
        "parent_decision_hash": parent_decision_hash,
        "parent_input_manifest_hash": parent_input_manifest_hash,
        "parent_signal_data_cutoff": require_aware_utc(parent_signal_data_cutoff),
        "candidate_signal_data_cutoff": require_aware_utc(
            candidate_signal_data_cutoff
        ),
        "candidate_artifact_hash": candidate_artifact_hash,
        "prospective_source_manifest_hash": prospective_source_manifest_hash,
        "champion_target_hash": champion_target.target_hash,
        "challenger_target_hash": challenger_target.target_hash,
        "quote_bundle_hash": quote_bundle.bundle_hash,
        "quote_manifest_hash": quote_bundle.quote_manifest_hash,
        "quote_id_by_symbol": {
            item.instrument_id: item.quote_id for item in quote_bundle.quotes
        },
        "quote_source_hash_by_symbol": dict(
            sorted(quote_source_hash_by_symbol.items())
        ),
        "adv_source_bar_ids_by_symbol": {
            symbol: tuple(sorted(bar_ids))
            for symbol, bar_ids in sorted(adv_source_bar_ids_by_symbol.items())
        },
        "decision_available_at": require_aware_utc(decision_available_at),
        "quote_as_of": quote_bundle.as_of,
        "recorded_at": require_aware_utc(recorded_at),
        "deterministic_primary_replay_match": True,
        "market_stream_connected": True,
        "trusted_for_promotion_evidence": True,
        "automatic_promotion_enabled": False,
        "real_order_routing": False,
    }
    return ProspectiveShadowCycleSourceV1.model_validate(
        {**payload, "provenance_hash": canonical_hash(payload)}
    )


def _valid_symbol(value: str) -> bool:
    return re.fullmatch(SYMBOL_PATTERN, value) is not None


__all__ = [
    "PROSPECTIVE_SHADOW_SOURCE_AGGREGATE",
    "TRUSTED_SHADOW_CYCLE_EVENT",
    "UNATTESTED_SHADOW_CYCLE_EVENT",
    "ProspectiveShadowCycleSourceV1",
    "build_prospective_shadow_cycle_source",
]
