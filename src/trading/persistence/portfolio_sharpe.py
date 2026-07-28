from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from trading.domain.contracts import model_payload
from trading.persistence.models import (
    ChallengerManifestRow,
    OosBudgetReservationRow,
    OosLockboxResultRow,
    PortfolioComparisonContractRow,
    ResearchCandidateArtifactRow,
)
from trading.research.portfolio_delta_sharpe import (
    PortfolioComparisonContractV1,
)


class PortfolioSharpePersistenceError(RuntimeError):
    """Raised when immutable portfolio-comparison persistence fails closed."""


class PortfolioSharpeRepository:
    """Append-only authority for pre-OOS portfolio integration contracts."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def store_comparison_contract(
        self,
        *,
        challenger_id: str,
        contract: PortfolioComparisonContractV1,
    ) -> bool:
        with self._session_factory.begin() as session:
            self._lock_challenger(session, challenger_id)
            challenger = session.get(ChallengerManifestRow, challenger_id)
            if challenger is None:
                raise PortfolioSharpePersistenceError("unknown Challenger")
            artifact = session.scalar(
                select(ResearchCandidateArtifactRow).where(
                    ResearchCandidateArtifactRow.challenger_id == challenger_id,
                    ResearchCandidateArtifactRow.bundle_hash
                    == contract.candidate_artifact_hash,
                )
            )
            if artifact is None:
                raise PortfolioSharpePersistenceError(
                    "portfolio contract Candidate artifact is not registered"
                )
            existing = session.get(
                PortfolioComparisonContractRow,
                contract.comparison_contract_id,
            )
            if existing is not None:
                if (
                    existing.challenger_id != challenger_id
                    or existing.contract_hash != contract.contract_hash
                ):
                    raise PortfolioSharpePersistenceError(
                        "portfolio comparison contract identity conflict"
                    )
                self._validate_row(existing, contract)
                return False
            existing_for_artifact = session.scalar(
                select(PortfolioComparisonContractRow).where(
                    PortfolioComparisonContractRow.challenger_id
                    == challenger_id,
                    PortfolioComparisonContractRow.candidate_artifact_hash
                    == contract.candidate_artifact_hash,
                )
            )
            if existing_for_artifact is not None:
                raise PortfolioSharpePersistenceError(
                    "Candidate artifact already has a portfolio contract"
                )
            oos_started = session.scalar(
                select(OosBudgetReservationRow.reservation_id)
                .where(OosBudgetReservationRow.challenger_id == challenger_id)
                .limit(1)
            )
            oos_result = session.scalar(
                select(OosLockboxResultRow.oos_result_id)
                .where(OosLockboxResultRow.challenger_id == challenger_id)
                .limit(1)
            )
            if oos_started is not None or oos_result is not None:
                raise PortfolioSharpePersistenceError(
                    "portfolio allocation cannot change after OOS begins"
                )
            row = PortfolioComparisonContractRow(
                comparison_contract_id=contract.comparison_contract_id,
                challenger_id=challenger_id,
                candidate_artifact_hash=contract.candidate_artifact_hash,
                champion_portfolio_manifest_hash=(
                    contract.champion_portfolio_manifest_hash
                ),
                candidate_portfolio_manifest_hash=(
                    contract.candidate_portfolio_manifest_hash
                ),
                allocation_policy_hash=contract.allocation_policy_hash,
                weight_selection_data_cutoff=(
                    contract.weight_selection_data_cutoff
                ),
                allocation_policy_created_at=(
                    contract.allocation_policy_created_at
                ),
                contract_hash=contract.contract_hash,
                payload_json=model_payload(contract),
                created_at=contract.created_at,
            )
            session.add(row)
            try:
                session.flush()
            except IntegrityError as exc:
                raise PortfolioSharpePersistenceError(
                    "portfolio comparison contract conflict"
                ) from exc
            return True

    def comparison_contract(
        self,
        *,
        challenger_id: str,
    ) -> PortfolioComparisonContractV1 | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(PortfolioComparisonContractRow).where(
                    PortfolioComparisonContractRow.challenger_id
                    == challenger_id
                )
            )
            if row is None:
                return None
            try:
                contract = PortfolioComparisonContractV1.model_validate(
                    row.payload_json
                )
            except ValueError as exc:
                raise PortfolioSharpePersistenceError(
                    "stored portfolio comparison contract is invalid"
                ) from exc
            self._validate_row(row, contract)
            return contract

    def status(self) -> dict[str, object]:
        with self._session_factory() as session:
            count = session.scalar(
                select(func.count()).select_from(
                    PortfolioComparisonContractRow
                )
            )
            latest = session.scalar(
                select(PortfolioComparisonContractRow)
                .order_by(
                    PortfolioComparisonContractRow.created_at.desc(),
                    PortfolioComparisonContractRow.comparison_contract_id.desc(),
                )
                .limit(1)
            )
            return {
                "schema_version": "portfolio_sharpe_ledger_status_v1",
                "comparison_contract_count": int(count or 0),
                "latest_comparison_contract_id": (
                    None if latest is None else latest.comparison_contract_id
                ),
                "latest_contract_hash": (
                    None if latest is None else latest.contract_hash
                ),
                "checked_at": datetime.now(UTC).isoformat(),
            }

    @staticmethod
    def _lock_challenger(session: Session, challenger_id: str) -> None:
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:scope))"),
                {"scope": f"portfolio-comparison:{challenger_id}"},
            )

    @staticmethod
    def _validate_row(
        row: PortfolioComparisonContractRow,
        contract: PortfolioComparisonContractV1,
    ) -> None:
        if (
            row.comparison_contract_id != contract.comparison_contract_id
            or row.candidate_artifact_hash != contract.candidate_artifact_hash
            or row.champion_portfolio_manifest_hash
            != contract.champion_portfolio_manifest_hash
            or row.candidate_portfolio_manifest_hash
            != contract.candidate_portfolio_manifest_hash
            or row.allocation_policy_hash != contract.allocation_policy_hash
            or row.contract_hash != contract.contract_hash
        ):
            raise PortfolioSharpePersistenceError(
                "stored portfolio comparison columns do not match payload"
            )


__all__ = [
    "PortfolioSharpePersistenceError",
    "PortfolioSharpeRepository",
]
