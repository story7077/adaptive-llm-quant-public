from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel

from trading.domain.hashing import canonical_hash
from trading.domain.time import require_aware_utc
from trading.persistence.research import (
    ResearchPersistenceError,
    ResearchRepository,
)
from trading.research.contracts import (
    AlgorithmProposalV1,
    AvailableDataCatalogV1,
    ChallengerManifestV1,
    CommanderSelectionV1,
    ResearchDecisionV1,
    ResearchRequestV1,
)
from trading.research.evidence import ResearchEvidenceBundleV1
from trading.research.host import SAFE_COMPONENT, ResearchPlaneHost

MAX_JSON_BYTES = 16 * 1024 * 1024
REPOSITORY_LOCAL_OUTPUT_ROOTS = frozenset({".local", "artifacts", "runs"})

class ResearchFileRuntimeError(RuntimeError):
    pass


def load_json_model[ModelT: BaseModel](
    path: Path,
    model_type: type[ModelT],
) -> ModelT:
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ResearchFileRuntimeError("JSON input must be a regular file")
    size = resolved.stat().st_size
    if size <= 0 or size > MAX_JSON_BYTES:
        raise ResearchFileRuntimeError(
            f"JSON input size must be between 1 and {MAX_JSON_BYTES} bytes"
        )
    try:
        raw = resolved.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ResearchFileRuntimeError("JSON input must be UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise ResearchFileRuntimeError(
            f"invalid JSON at line {exc.lineno}, column {exc.colno}"
        ) from exc
    if not isinstance(payload, Mapping):
        raise ResearchFileRuntimeError("JSON input must contain one object")
    return model_type.model_validate(payload)


def atomic_write_json(path: Path, value: object) -> bool:
    destination = path.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    normalized = _json_value(value)
    serialized = json.dumps(
        normalized,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    encoded = serialized.encode("utf-8")
    if len(encoded) > MAX_JSON_BYTES:
        raise ResearchFileRuntimeError(
            f"JSON output exceeds {MAX_JSON_BYTES} bytes"
        )
    if destination.exists():
        existing = load_json_object(destination)
        if canonical_hash(existing) != canonical_hash(normalized):
            raise ResearchFileRuntimeError("JSON output path contains conflicting data")
        return False
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return True


def load_json_object(path: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ResearchFileRuntimeError("JSON input must be a regular file")
    size = resolved.stat().st_size
    if size <= 0 or size > MAX_JSON_BYTES:
        raise ResearchFileRuntimeError(
            f"JSON input size must be between 1 and {MAX_JSON_BYTES} bytes"
        )
    try:
        payload = json.loads(resolved.read_bytes().decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ResearchFileRuntimeError("JSON input must be UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise ResearchFileRuntimeError(
            f"invalid JSON at line {exc.lineno}, column {exc.colno}"
        ) from exc
    if not isinstance(payload, dict):
        raise ResearchFileRuntimeError("JSON input must contain one object")
    return cast(dict[str, Any], payload)


def resolve_local_output(path: Path, *, repository_root: Path) -> Path:
    root = repository_root.resolve()
    resolved = (root / path).resolve() if not path.is_absolute() else path.resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        return resolved
    allowed = bool(relative.parts) and (
        relative.parts[0] in REPOSITORY_LOCAL_OUTPUT_ROOTS
        or relative.parts[:2] == ("data", "raw")
    )
    if not allowed:
        raise ResearchFileRuntimeError(
            "repository-local Research output must be under .local/, artifacts/, "
            "runs/, or data/raw/"
        )
    return resolved


def local_artifact_label(path: Path, *, repository_root: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(repository_root.resolve()).as_posix()
    except ValueError:
        return f"EXTERNAL_LOCAL/{resolved.name}"


def write_cycle_decision(
    *,
    bundle_root: Path,
    request: ResearchRequestV1,
    decision: ResearchDecisionV1,
    current_selection: CommanderSelectionV1,
) -> tuple[Path, bool]:
    if not SAFE_COMPONENT.fullmatch(request.research_cycle_id):
        raise ResearchFileRuntimeError("unsafe research_cycle_id")
    cycle_root = bundle_root.resolve() / request.research_cycle_id
    stored_request_path = cycle_root / "request" / "research_request.json"
    try:
        stored_request = load_json_model(stored_request_path, ResearchRequestV1)
    except (OSError, ResearchFileRuntimeError) as exc:
        raise ResearchFileRuntimeError(
            "prepared cycle request artifact is unavailable"
        ) from exc
    if canonical_hash(stored_request) != canonical_hash(request):
        raise ResearchFileRuntimeError("stored Research request hash mismatch")
    try:
        decision.assert_bound_to(
            request,
            received_at=decision.created_at,
            current_selection=current_selection,
        )
    except ValueError as exc:
        raise ResearchFileRuntimeError(
            "validated Commander decision no longer matches the active cycle"
        ) from exc
    destination = cycle_root / "output" / "research_decision.json"
    created = atomic_write_json(destination, decision)
    return destination, created


class ResearchPlaneFileRuntime:
    def __init__(
        self,
        *,
        repository: ResearchRepository,
        bundle_root: Path,
    ) -> None:
        self._repository = repository
        self._host = ResearchPlaneHost(
            repository=repository,
            bundle_root=bundle_root,
        )

    def prepare_cycle(self, request_file: Path) -> tuple[ResearchRequestV1, Path]:
        request = load_json_model(request_file, ResearchRequestV1)
        cycle_root = self._host.prepare_cycle(request)
        expected = cycle_root / "request" / "research_request.json"
        if not expected.is_file():
            raise ResearchFileRuntimeError(
                "cycle was persisted but its immutable request artifact is unavailable"
            )
        stored = load_json_model(expected, ResearchRequestV1)
        if canonical_hash(stored) != canonical_hash(request):
            raise ResearchFileRuntimeError("stored Research request hash mismatch")
        return request, cycle_root

    def import_evidence(
        self,
        *,
        request_file: Path,
        evidence_file: Path,
        imported_at: datetime,
    ) -> tuple[ResearchEvidenceBundleV1, bool]:
        request = load_json_model(request_file, ResearchRequestV1)
        evidence = load_json_model(evidence_file, ResearchEvidenceBundleV1)
        _require_evidence_binding(request, evidence)
        timestamp = require_aware_utc(imported_at)
        if timestamp < evidence.captured_at:
            raise ResearchFileRuntimeError(
                "evidence import cannot predate evidence capture"
            )
        created = self._repository.store_evidence_bundle(
            evidence,
            created_at=timestamp,
        )
        return evidence, created

    def import_decision(
        self,
        *,
        request_file: Path,
        decision_file: Path,
        catalog_file: Path,
        evidence_file: Path,
        received_at: datetime,
    ) -> tuple[ResearchDecisionV1, str | None]:
        request = load_json_model(request_file, ResearchRequestV1)
        decision = load_json_model(decision_file, ResearchDecisionV1)
        catalog = load_json_model(catalog_file, AvailableDataCatalogV1)
        evidence = load_json_model(evidence_file, ResearchEvidenceBundleV1)
        _require_catalog_binding(request, catalog)
        _require_evidence_binding(request, evidence)
        timestamp = require_aware_utc(received_at)
        if timestamp < evidence.captured_at or timestamp < decision.created_at:
            raise ResearchFileRuntimeError(
                "decision receipt cannot predate its decision or evidence"
            )
        self._repository.store_evidence_bundle(
            evidence,
            created_at=timestamp,
        )
        proposal_id = self._host.accept_decision(
            decision,
            request=request,
            catalog=catalog,
            evidence_bundle=evidence,
            received_at=timestamp,
        )
        return decision, proposal_id

    def register_challenger(
        self,
        *,
        decision_file: Path,
        manifest_file: Path,
    ) -> tuple[ChallengerManifestV1, AlgorithmProposalV1, bool]:
        decision = load_json_model(decision_file, ResearchDecisionV1)
        manifest = load_json_model(manifest_file, ChallengerManifestV1)
        proposal = decision.proposal
        if proposal is None:
            raise ResearchFileRuntimeError(
                "Challenger registration requires an accepted proposal decision"
            )
        accepted_proposal = self._accepted_proposal(proposal.proposal_id)
        if canonical_hash(accepted_proposal) != canonical_hash(proposal):
            raise ResearchFileRuntimeError(
                "decision proposal differs from the accepted append-only proposal"
            )
        _require_challenger_binding(
            manifest=manifest,
            proposal=proposal,
            decision=decision,
        )
        created = self._repository.register_challenger(
            manifest,
            proposal_id=proposal.proposal_id,
        )
        return manifest, proposal, created

    def _accepted_proposal(self, proposal_id: str) -> AlgorithmProposalV1:
        try:
            proposal = self._repository.get_proposal(proposal_id)
        except ResearchPersistenceError as exc:
            raise ResearchFileRuntimeError(str(exc)) from exc
        if proposal is None:
            raise ResearchFileRuntimeError(
                "Challenger proposal is not present in the append-only "
                "Research registry"
            )
        return proposal


def _require_catalog_binding(
    request: ResearchRequestV1,
    catalog: AvailableDataCatalogV1,
) -> None:
    request_catalog = AvailableDataCatalogV1.model_validate(
        request.available_data_catalog
    )
    if canonical_hash(request_catalog) != canonical_hash(catalog):
        raise ResearchFileRuntimeError(
            "catalog differs from the version bound to the Research request"
        )


def _require_evidence_binding(
    request: ResearchRequestV1,
    evidence: ResearchEvidenceBundleV1,
) -> None:
    if evidence.research_cycle_id != request.research_cycle_id:
        raise ResearchFileRuntimeError("evidence belongs to another Research cycle")
    expected_hash = canonical_hash(evidence)
    referenced_hashes = {
        value
        for item in request.recent_web_research
        if isinstance((value := item.get("evidence_bundle_hash")), str)
    }
    if expected_hash not in referenced_hashes:
        raise ResearchFileRuntimeError(
            "evidence hash is not bound to the Research request"
        )


def _require_challenger_binding(
    *,
    manifest: ChallengerManifestV1,
    proposal: AlgorithmProposalV1,
    decision: ResearchDecisionV1,
) -> None:
    bindings: tuple[tuple[str, object, object], ...] = (
        ("proposal_hash", manifest.proposal_hash, proposal.proposal_hash),
        ("strategy_id", manifest.strategy_id, proposal.proposed_strategy_id),
        (
            "strategy_version",
            manifest.strategy_version,
            proposal.proposed_strategy_version,
        ),
        ("parent_version", manifest.parent_version, proposal.parent_strategy_version),
        ("hypothesis_id", manifest.hypothesis_id, proposal.hypothesis_id),
        ("experiment_family", manifest.experiment_family, decision.experiment_family),
        (
            "created_by_commander",
            manifest.created_by_commander,
            decision.selected_commander,
        ),
        (
            "evidence_source_ids",
            sorted(manifest.evidence_source_ids),
            sorted(proposal.evidence_source_ids),
        ),
        ("required_data", manifest.required_data, proposal.required_data),
        ("decision_horizon", manifest.decision_horizon, proposal.target_horizon),
        (
            "execution_universe",
            manifest.execution_universe,
            proposal.target_universe,
        ),
        ("estimated_turnover", manifest.estimated_turnover, proposal.estimated_turnover),
        ("estimated_capacity", manifest.estimated_capacity, proposal.estimated_capacity),
    )
    mismatches = [name for name, actual, expected in bindings if actual != expected]
    if mismatches:
        raise ResearchFileRuntimeError(
            "Challenger manifest does not match its accepted proposal: "
            + ",".join(mismatches)
        )


def _json_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value
