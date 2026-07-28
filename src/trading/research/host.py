from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from pydantic import JsonValue

from trading.domain.hashing import canonical_hash
from trading.domain.time import require_aware_utc
from trading.persistence.experiment_outcomes import ExperimentOutcomeRepository
from trading.persistence.meta_controller import MetaControllerRepository
from trading.persistence.research import ResearchRepository
from trading.research.contracts import (
    AvailableDataCatalogV1,
    CommanderSelectionV1,
    ResearchDecisionV1,
    ResearchRequestV1,
)
from trading.research.evidence import ResearchEvidenceBundleV1
from trading.research.proposal import (
    require_shadow_execution_support,
    validate_proposal_against_catalog,
)
from trading.research.v2_contracts import (
    ResearchDecisionV2,
    ResearchRequestV2,
)

SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
FORBIDDEN_PAYLOAD_KEYS = {
    "api_key",
    "secret",
    "secret_key",
    "password",
    "cookie",
    "authorization",
    "account_id",
    "browser_profile",
    "user_data_dir",
}


class ResearchHostError(RuntimeError):
    pass


def build_research_request(
    *,
    request_id: str,
    research_cycle_id: str,
    commander_selection: CommanderSelectionV1,
    created_at: datetime,
    as_of: datetime,
    data_available_cutoff: datetime,
    expires_at: datetime,
    source_snapshot_commit: str,
    champion_version: str,
    experiment_family: str,
    champion_manifest: dict[str, JsonValue],
    active_challenger_manifests: list[dict[str, JsonValue]],
    strategy_performance_summary: dict[str, JsonValue],
    failure_case_clusters: list[dict[str, JsonValue]],
    regime_summary: dict[str, JsonValue],
    execution_cost_summary: dict[str, JsonValue],
    capacity_summary: dict[str, JsonValue],
    recent_market_evidence: list[dict[str, JsonValue]],
    recent_web_research: list[dict[str, JsonValue]],
    available_data_catalog: AvailableDataCatalogV1,
    allowed_change_scope: list[str],
    forbidden_change_scope: list[str],
    experiment_budget: dict[str, JsonValue],
) -> ResearchRequestV1:
    request_created_at = require_aware_utc(created_at)
    if (
        commander_selection.created_at > request_created_at
        or commander_selection.effective_at > request_created_at
    ):
        raise ResearchHostError(
            "Commander selection is not available and effective at request creation"
        )
    payload: dict[str, Any] = {
        "schema_version": "research_request_v1",
        "request_id": request_id,
        "research_cycle_id": research_cycle_id,
        "selected_commander": commander_selection.selected_commander,
        "commander_selection_id": commander_selection.selection_id,
        "commander_selection_version": commander_selection.version,
        "created_at": request_created_at,
        "as_of": require_aware_utc(as_of),
        "data_available_cutoff": require_aware_utc(data_available_cutoff),
        "expires_at": require_aware_utc(expires_at),
        "source_snapshot_commit": source_snapshot_commit,
        "champion_version": champion_version,
        "experiment_family": experiment_family,
        "champion_manifest": champion_manifest,
        "active_challenger_manifests": active_challenger_manifests,
        "strategy_performance_summary": strategy_performance_summary,
        "failure_case_clusters": failure_case_clusters,
        "regime_summary": regime_summary,
        "execution_cost_summary": execution_cost_summary,
        "capacity_summary": capacity_summary,
        "recent_market_evidence": recent_market_evidence,
        "recent_web_research": recent_web_research,
        "available_data_catalog": available_data_catalog.model_dump(mode="json"),
        "allowed_change_scope": allowed_change_scope,
        "forbidden_change_scope": forbidden_change_scope,
        "experiment_budget": experiment_budget,
    }
    _reject_sensitive_payload(payload)
    payload["context_manifest_hash"] = canonical_hash(payload)
    return ResearchRequestV1.model_validate(payload)


def build_research_request_v2(
    *,
    outcome_repository: ExperimentOutcomeRepository,
    meta_controller_repository: MetaControllerRepository,
    snapshot_id: str,
    action_plan_id: str,
    request_id: str,
    research_cycle_id: str,
    commander_selection: CommanderSelectionV1,
    created_at: datetime,
    as_of: datetime,
    data_available_cutoff: datetime,
    expires_at: datetime,
    source_snapshot_commit: str,
    champion_version: str,
    experiment_family: str,
    champion_manifest: dict[str, JsonValue],
    active_challenger_manifests: list[dict[str, JsonValue]],
    execution_cost_summary: dict[str, JsonValue],
    capacity_summary: dict[str, JsonValue],
    recent_market_evidence: list[dict[str, JsonValue]],
    recent_web_research: list[dict[str, JsonValue]],
    available_data_catalog: AvailableDataCatalogV1,
    allowed_change_scope: list[str],
    forbidden_change_scope: list[str],
    experiment_budget: dict[str, JsonValue],
) -> ResearchRequestV2:
    """Build V2 only from persisted recursive artifacts, never caller summaries."""

    snapshot = outcome_repository.get_memory_snapshot(snapshot_id)
    if snapshot is None:
        raise ResearchHostError("unknown immutable research memory snapshot")
    plan = meta_controller_repository.get_plan(action_plan_id)
    if plan is None:
        raise ResearchHostError("unknown immutable research action plan")
    if plan.research_cycle_id != research_cycle_id:
        raise ResearchHostError("research action plan belongs to another cycle")
    if plan.research_memory_snapshot_hash != snapshot.snapshot_hash:
        raise ResearchHostError("action plan and memory snapshot are not bound")
    request_created_at = require_aware_utc(created_at)
    if (
        commander_selection.created_at > request_created_at
        or commander_selection.effective_at > request_created_at
    ):
        raise ResearchHostError(
            "Commander selection is not available and effective at request creation"
        )
    remaining_submissions = _remaining_submission_budget(experiment_budget)
    if plan.maximum_total_submissions > remaining_submissions:
        raise ResearchHostError(
            "research action plan exceeds the remaining submission budget"
        )
    payload: dict[str, Any] = {
        "schema_version": "research_request_v2",
        "request_id": request_id,
        "research_cycle_id": research_cycle_id,
        "selected_commander": commander_selection.selected_commander,
        "commander_selection_id": commander_selection.selection_id,
        "commander_selection_version": commander_selection.version,
        "created_at": request_created_at,
        "as_of": require_aware_utc(as_of),
        "data_available_cutoff": require_aware_utc(data_available_cutoff),
        "expires_at": require_aware_utc(expires_at),
        "source_snapshot_commit": source_snapshot_commit,
        "champion_version": champion_version,
        "experiment_family": experiment_family,
        "champion_manifest": champion_manifest,
        "active_challenger_manifests": active_challenger_manifests,
        "strategy_performance_summary": {
            "source": "research_memory_snapshot_v1",
            "snapshot_hash": snapshot.snapshot_hash,
            "action_statistics": [
                item.model_dump(mode="json")
                for item in snapshot.action_statistics
            ],
            "prediction_calibration_summary": (
                snapshot.prediction_calibration_summary.model_dump(
                    mode="json"
                )
            ),
        },
        "failure_case_clusters": [
            item.model_dump(mode="json")
            for item in snapshot.recent_failure_clusters
        ],
        "regime_summary": {
            "source": "research_action_plan_v1",
            "context": plan.context.model_dump(mode="json"),
            "regime_action_statistics": [
                item.model_dump(mode="json")
                for item in snapshot.regime_action_statistics
            ],
        },
        "execution_cost_summary": execution_cost_summary,
        "capacity_summary": capacity_summary,
        "recent_market_evidence": recent_market_evidence,
        "recent_web_research": recent_web_research,
        "available_data_catalog": available_data_catalog.model_dump(mode="json"),
        "allowed_change_scope": allowed_change_scope,
        "forbidden_change_scope": forbidden_change_scope,
        "experiment_budget": experiment_budget,
        "research_memory_snapshot": snapshot,
        "research_action_plan": plan,
    }
    _reject_sensitive_payload(payload)
    payload["context_manifest_hash"] = canonical_hash(payload)
    return ResearchRequestV2.model_validate(payload)


class ResearchPlaneHost:
    def __init__(
        self,
        *,
        repository: ResearchRepository,
        bundle_root: Path,
    ) -> None:
        self._repository = repository
        self._bundle_root = bundle_root.resolve()

    def prepare_cycle(
        self,
        request: ResearchRequestV1 | ResearchRequestV2,
    ) -> Path:
        if not SAFE_COMPONENT.fullmatch(request.research_cycle_id):
            raise ResearchHostError("unsafe research_cycle_id")
        current_selection = self._repository.current_selection()
        if current_selection is None:
            raise ResearchHostError("No Research Commander selection exists")
        try:
            request.assert_current_selection(current_selection)
        except ValueError as exc:
            raise ResearchHostError("STALE_SELECTION") from exc
        created = self._repository.create_cycle(request)
        cycle_root = self._bundle_root / request.research_cycle_id
        request_root = cycle_root / "request"
        request_root.mkdir(parents=True, exist_ok=True)
        destination = request_root / "research_request.json"
        serialized = json.dumps(
            request.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        if destination.exists():
            existing = json.loads(destination.read_text(encoding="utf-8"))
            if canonical_hash(existing) != canonical_hash(
                request.model_dump(mode="json")
            ):
                raise ResearchHostError("research request file hash conflict")
        elif created:
            _atomic_write(destination, serialized)
        return cycle_root

    def accept_decision(
        self,
        decision: ResearchDecisionV1,
        *,
        request: ResearchRequestV1,
        catalog: AvailableDataCatalogV1,
        evidence_bundle: ResearchEvidenceBundleV1,
        received_at: datetime,
    ) -> str | None:
        if evidence_bundle.research_cycle_id != request.research_cycle_id:
            raise ResearchHostError("evidence bundle belongs to another cycle")
        expected_evidence_hash = canonical_hash(evidence_bundle)
        referenced_hashes = {
            value
            for item in request.recent_web_research
            if isinstance((value := item.get("evidence_bundle_hash")), str)
        }
        if expected_evidence_hash not in referenced_hashes:
            raise ResearchHostError(
                "Commander request does not bind the supplied evidence bundle"
            )
        if decision.proposal is not None:
            validate_proposal_against_catalog(
                decision.proposal,
                catalog=catalog,
                evidence_source_ids={
                    source.source_id for source in evidence_bundle.sources
                },
                request=request,
            )
            require_shadow_execution_support(decision.proposal, catalog=catalog)
        return self._repository.accept_decision(
            decision,
            received_at=received_at,
        )

    def accept_decision_v2(
        self,
        decision: ResearchDecisionV2,
        *,
        request: ResearchRequestV2,
        catalog: AvailableDataCatalogV1,
        evidence_bundle: ResearchEvidenceBundleV1,
        received_at: datetime,
    ) -> str | None:
        if evidence_bundle.research_cycle_id != request.research_cycle_id:
            raise ResearchHostError("evidence bundle belongs to another cycle")
        expected_evidence_hash = canonical_hash(evidence_bundle)
        referenced_hashes = {
            value
            for item in request.recent_web_research
            if isinstance((value := item.get("evidence_bundle_hash")), str)
        }
        if expected_evidence_hash not in referenced_hashes:
            raise ResearchHostError(
                "Commander request does not bind the supplied evidence bundle"
            )
        if decision.proposal is not None:
            if (
                decision.proposal.primary_action_kind
                not in request.research_action_plan.permitted_action_kinds()
            ):
                raise ResearchHostError(
                    "Commander proposal action is outside the action plan"
                )
            validate_proposal_against_catalog(
                decision.proposal,
                catalog=catalog,
                evidence_source_ids={
                    source.source_id for source in evidence_bundle.sources
                },
                request=request,
            )
            require_shadow_execution_support(
                decision.proposal,
                catalog=catalog,
            )
        return self._repository.accept_decision_v2(
            decision,
            received_at=received_at,
        )

    def status(self) -> dict[str, Any]:
        return {
            **self._repository.status(),
            "research_plane": "adaptive_research_plane_v1",
            "operational_plane_isolation": True,
            "automatic_promotion_enabled": False,
            "real_order_routing": False,
        }


def evidence_manifest_for_request(
    bundle: ResearchEvidenceBundleV1,
) -> dict[str, JsonValue]:
    return {
        "schema_version": bundle.schema_version,
        "research_cycle_id": bundle.research_cycle_id,
        "evidence_bundle_hash": canonical_hash(bundle),
        "data_available_cutoff": bundle.data_available_cutoff.isoformat(),
        "source_ids": [source.source_id for source in bundle.sources],
        "claim_ids": [claim.claim_id for claim in bundle.claims],
        "sources": [
            source.model_dump(mode="json")
            for source in bundle.sources
        ],
        "claims": [
            claim.model_dump(mode="json")
            for claim in bundle.claims
        ],
    }


def _atomic_write(destination: Path, content: str) -> None:
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        dir=destination.parent,
        text=True,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        Path(temp_name).replace(destination)
    except Exception:
        Path(temp_name).unlink(missing_ok=True)
        raise


def _reject_sensitive_payload(value: object, *, path: str = "$") -> None:
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        for key, nested in mapping.items():
            normalized = str(key).strip().lower()
            if normalized in FORBIDDEN_PAYLOAD_KEYS:
                raise ResearchHostError(f"sensitive field rejected at {path}.{key}")
            _reject_sensitive_payload(nested, path=f"{path}.{key}")
    elif isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        sequence = cast(Sequence[object], value)
        for index, nested in enumerate(sequence):
            _reject_sensitive_payload(nested, path=f"{path}[{index}]")


def _remaining_submission_budget(
    experiment_budget: dict[str, JsonValue],
) -> int:
    limit = experiment_budget.get("family_submission_limit")
    used = experiment_budget.get("family_submissions_used")
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not isinstance(used, int)
        or isinstance(used, bool)
        or used < 0
        or limit <= used
    ):
        raise ResearchHostError("experiment-family submission budget is exhausted")
    return limit - used
