from __future__ import annotations

from collections.abc import Collection

from trading.research.contracts import (
    AlgorithmProposalV1,
    AvailableDataCatalogV1,
    ResearchRequestV1,
)
from trading.research.experiment_outcomes import AlgorithmProposalV2
from trading.research.v2_contracts import ResearchRequestV2


class ProposalValidationError(ValueError):
    pass


def validate_proposal_against_catalog(
    proposal: AlgorithmProposalV1 | AlgorithmProposalV2,
    *,
    catalog: AvailableDataCatalogV1,
    evidence_source_ids: Collection[str],
    request: ResearchRequestV1 | ResearchRequestV2 | None = None,
) -> None:
    catalog_by_symbol = {item.symbol: item for item in catalog.instruments}
    missing = sorted(set(proposal.target_universe) - set(catalog_by_symbol))
    if missing:
        raise ProposalValidationError(
            "target universe is outside the versioned data catalog: "
            + ",".join(missing)
        )
    unsupported_evidence = sorted(
        set(proposal.evidence_source_ids) - set(evidence_source_ids)
    )
    if unsupported_evidence:
        raise ProposalValidationError(
            "proposal cites evidence outside the current request: "
            + ",".join(unsupported_evidence)
        )
    missing_data: list[str] = []
    for symbol in proposal.target_universe:
        instrument = catalog_by_symbol[symbol]
        if instrument.daily_history_sessions <= 0:
            missing_data.append(f"{symbol}:NO_DAILY_HISTORY")
        if (
            instrument.asset_class == "US_EQUITY"
            and not instrument.point_in_time_membership_available
        ):
            missing_data.append(f"{symbol}:NO_PIT_MEMBERSHIP")
    if missing_data:
        raise ProposalValidationError(
            "target universe lacks mandatory research data: "
            + ",".join(missing_data)
        )
    if request is not None:
        _validate_proposal_against_request(proposal, request)


def require_shadow_execution_support(
    proposal: AlgorithmProposalV1 | AlgorithmProposalV2,
    *,
    catalog: AvailableDataCatalogV1,
) -> None:
    catalog_by_symbol = {item.symbol: item for item in catalog.instruments}
    blocked = sorted(
        symbol
        for symbol in proposal.target_universe
        if symbol not in catalog_by_symbol
        or not catalog_by_symbol[symbol].execution_supported
    )
    if blocked:
        raise ProposalValidationError(
            "shadow execution is unsupported for: " + ",".join(blocked)
        )


def _validate_proposal_against_request(
    proposal: AlgorithmProposalV1 | AlgorithmProposalV2,
    request: ResearchRequestV1 | ResearchRequestV2,
) -> None:
    champion_strategy_id = request.champion_manifest.get("strategy_id")
    if (
        isinstance(champion_strategy_id, str)
        and proposal.parent_strategy_id != champion_strategy_id
    ):
        raise ProposalValidationError(
            "proposal parent strategy does not match the current Champion"
        )
    if proposal.parent_strategy_version != request.champion_version:
        raise ProposalValidationError(
            "proposal parent version does not match the current Champion"
        )
    forbidden = tuple(_scope_prefix(item) for item in request.forbidden_change_scope)
    allowed = tuple(_scope_prefix(item) for item in request.allowed_change_scope)
    violations = [
        path
        for path in proposal.files_allowed_to_change
        if not any(path.startswith(prefix) for prefix in allowed)
        or any(path.startswith(prefix) for prefix in forbidden)
    ]
    if violations:
        raise ProposalValidationError(
            "proposal file scope exceeds the Commander request: "
            + ",".join(sorted(violations))
        )


def _scope_prefix(value: str) -> str:
    normalized = value.replace("\\", "/").strip().lstrip("./")
    if not normalized or ".." in normalized.split("/"):
        raise ProposalValidationError("request contains an unsafe file scope")
    return normalized if normalized.endswith("/") else normalized + "/"
