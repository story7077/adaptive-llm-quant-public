from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from trading.domain.contracts import DomainModel
from trading.domain.hashing import canonical_hash
from trading.research.contracts import (
    HASH_PATTERN,
    IDENTIFIER_PATTERN,
    VERSION_PATTERN,
    AlgorithmProposalV1,
    ChallengerManifestV1,
    ResearchCommanderKind,
    ResearchRequestV1,
)
from trading.research.experiment_outcomes import AlgorithmProposalV2
from trading.research.v2_contracts import ResearchRequestV2


class CandidateRequestBindingV1(DomainModel):
    request_id: str = Field(pattern=IDENTIFIER_PATTERN)
    research_cycle_id: str = Field(pattern=IDENTIFIER_PATTERN)
    context_manifest_hash: str = Field(pattern=HASH_PATTERN)
    source_snapshot_commit: str = Field(pattern=r"^[a-f0-9]{40,64}$")
    champion_version: str = Field(pattern=VERSION_PATTERN)
    experiment_family: str = Field(pattern=IDENTIFIER_PATTERN)
    selected_commander: ResearchCommanderKind
    commander_selection_id: str = Field(pattern=IDENTIFIER_PATTERN)
    commander_selection_version: int = Field(ge=1)


class CandidateRuntimeV1(DomainModel):
    implementation: str = Field(min_length=1, max_length=80)
    version: str = Field(min_length=1, max_length=80)
    abi_tag: str = Field(min_length=1, max_length=120)
    executable_sha256: str = Field(pattern=HASH_PATTERN)


class CandidateAbiV1(DomainModel):
    request_schema_version: Literal["candidate_decision_request_v1"]
    response_schema_version: Literal["candidate_decision_response_v1"]
    entrypoint_input: Literal["RAW_JSON_OBJECT"]
    entrypoint_output: Literal["RAW_JSON_OBJECT"]
    orders_permitted: Literal[False] = False
    fills_permitted: Literal[False] = False
    returns_or_pnl_permitted: Literal[False] = False


class CandidateArtifactBundleV1(DomainModel):
    """Immutable handoff from the isolated Builder to the trusted host."""

    schema_version: Literal["candidate_artifact_bundle_v1"] = "candidate_artifact_bundle_v1"
    bundle_id: str = Field(pattern=IDENTIFIER_PATTERN)
    challenger_id: str = Field(pattern=IDENTIFIER_PATTERN)
    request_binding: CandidateRequestBindingV1
    source_snapshot_hash: str = Field(pattern=HASH_PATTERN)
    candidate_tree_hash: str = Field(pattern=HASH_PATTERN)
    code_hash: str = Field(pattern=HASH_PATTERN)
    config_hash: str = Field(pattern=HASH_PATTERN)
    patch_hash: str = Field(pattern=HASH_PATTERN)
    proposal_hash: str = Field(pattern=HASH_PATTERN)
    builder_result_hash: str = Field(pattern=HASH_PATTERN)
    test_manifest_hash: str = Field(pattern=HASH_PATTERN)
    challenger_manifest_hash: str = Field(pattern=HASH_PATTERN)
    validation_request_hash: str = Field(pattern=HASH_PATTERN)
    runtime: CandidateRuntimeV1
    declared_entrypoint: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$")
    candidate_abi: CandidateAbiV1
    broker_access_permitted: Literal[False] = False
    credential_access_permitted: Literal[False] = False
    network_access_permitted: Literal[False] = False
    filesystem_write_permitted: Literal[False] = False
    real_order_routing: Literal[False] = False
    bundle_hash: str = Field(pattern=HASH_PATTERN)

    @model_validator(mode="after")
    def validate_hash(self) -> Self:
        payload = self.model_dump(mode="python", exclude={"bundle_hash"})
        if canonical_hash(payload) != self.bundle_hash:
            raise ValueError("Candidate artifact bundle hash mismatch")
        return self

    def assert_bound_to(
        self,
        *,
        request: ResearchRequestV1 | ResearchRequestV2,
        proposal: AlgorithmProposalV1 | AlgorithmProposalV2,
        manifest: ChallengerManifestV1,
    ) -> None:
        binding = self.request_binding
        request_matches = (
            binding.request_id == request.request_id
            and binding.research_cycle_id == request.research_cycle_id
            and binding.context_manifest_hash == request.context_manifest_hash
            and binding.source_snapshot_commit == request.source_snapshot_commit
            and binding.champion_version == request.champion_version
            and binding.experiment_family == request.experiment_family
            and binding.selected_commander is request.selected_commander
            and binding.commander_selection_id == request.commander_selection_id
            and binding.commander_selection_version == request.commander_selection_version
        )
        if not request_matches:
            raise ValueError("Candidate artifact request binding mismatch")
        manifest_matches = (
            self.challenger_id == manifest.challenger_id
            and self.challenger_manifest_hash == manifest.manifest_hash
            and self.code_hash == manifest.code_hash
            and self.config_hash == manifest.config_hash
            and self.patch_hash == manifest.patch_hash
            and self.proposal_hash == manifest.proposal_hash
            and self.test_manifest_hash == manifest.test_manifest_hash
            and manifest.source_commit == request.source_snapshot_commit
            and manifest.experiment_family == request.experiment_family
        )
        if not manifest_matches:
            raise ValueError("Candidate artifact Challenger binding mismatch")
        proposal_matches = (
            self.proposal_hash == proposal.proposal_hash
            and manifest.hypothesis_id == proposal.hypothesis_id
            and manifest.strategy_id == proposal.proposed_strategy_id
            and manifest.strategy_version == proposal.proposed_strategy_version
            and manifest.parent_version == proposal.parent_strategy_version
            and manifest.required_data == proposal.required_data
            and manifest.decision_horizon == proposal.target_horizon
            and manifest.execution_universe == proposal.target_universe
        )
        if not proposal_matches:
            raise ValueError("Candidate artifact proposal binding mismatch")


def build_candidate_artifact_bundle(
    *,
    bundle_id: str,
    challenger_id: str,
    request_binding: CandidateRequestBindingV1,
    source_snapshot_hash: str,
    candidate_tree_hash: str,
    code_hash: str,
    config_hash: str,
    patch_hash: str,
    proposal_hash: str,
    builder_result_hash: str,
    test_manifest_hash: str,
    challenger_manifest_hash: str,
    validation_request_hash: str,
    runtime: CandidateRuntimeV1,
    declared_entrypoint: str,
) -> CandidateArtifactBundleV1:
    payload = {
        "schema_version": "candidate_artifact_bundle_v1",
        "bundle_id": bundle_id,
        "challenger_id": challenger_id,
        "request_binding": request_binding,
        "source_snapshot_hash": source_snapshot_hash,
        "candidate_tree_hash": candidate_tree_hash,
        "code_hash": code_hash,
        "config_hash": config_hash,
        "patch_hash": patch_hash,
        "proposal_hash": proposal_hash,
        "builder_result_hash": builder_result_hash,
        "test_manifest_hash": test_manifest_hash,
        "challenger_manifest_hash": challenger_manifest_hash,
        "validation_request_hash": validation_request_hash,
        "runtime": runtime,
        "declared_entrypoint": declared_entrypoint,
        "candidate_abi": CandidateAbiV1(
            request_schema_version="candidate_decision_request_v1",
            response_schema_version="candidate_decision_response_v1",
            entrypoint_input="RAW_JSON_OBJECT",
            entrypoint_output="RAW_JSON_OBJECT",
        ),
        "broker_access_permitted": False,
        "credential_access_permitted": False,
        "network_access_permitted": False,
        "filesystem_write_permitted": False,
        "real_order_routing": False,
    }
    return CandidateArtifactBundleV1.model_validate(
        {**payload, "bundle_hash": canonical_hash(payload)}
    )
