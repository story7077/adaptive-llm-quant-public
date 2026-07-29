from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from trading.experiments.arms import ARM_IDS, states_are_independent
from trading.persistence.models import (
    FeatureSnapshotRow,
    LedgerPostingRow,
    LedgerTransactionRow,
    ProcessedEventRow,
    RunRow,
    ShadowArmRow,
)
from trading.replay.engine import replay_full
from trading.runtime.pipeline import load_arm_states


@dataclass(frozen=True, slots=True)
class VerificationReport:
    run_id: str
    passed: bool
    checks: dict[str, bool]
    original_hash: str
    replay_hash: str

    def as_payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "passed": self.passed,
            "checks": self.checks,
            "original_hash": self.original_hash,
            "replay_hash": self.replay_hash,
        }


def verify_run(
    session_factory: sessionmaker[Session],
    run_id: str,
) -> VerificationReport:
    replay = replay_full(session_factory, run_id)
    with session_factory() as session:
        run = session.get(RunRow, run_id)
        if run is None or run.result_hash is None or run.result_manifest is None:
            raise ValueError(f"Run is not completed: {run_id}")
        original_hash = run.result_hash
        ledger_balanced = _ledger_balanced(session, run_id)
        arm_count = session.scalar(
            select(func.count()).select_from(ShadowArmRow).where(ShadowArmRow.run_id == run_id)
        )
        processed_count = session.scalar(
            select(func.count())
            .select_from(ProcessedEventRow)
            .where(ProcessedEventRow.consumer_name == "phase0_feature_fixture_consumer_v1")
        )
        feature_count = session.scalar(select(func.count()).select_from(FeatureSnapshotRow))
        recovered_states = load_arm_states(session, run_id)

    invariants = replay.manifest["invariants"]
    replay_states = {
        arm_id: replay.artifacts.arms[arm_id].state for arm_id in ARM_IDS
    }
    checks = {
        "replay_hash_equal": original_hash == replay.result_hash,
        "manifest_equal": run.result_manifest == replay.manifest,
        "ledger_balanced": ledger_balanced,
        "seven_shadow_arms": arm_count == len(ARM_IDS),
        "arm_states_independent": states_are_independent(
            replay_states
        ),
        "arm_states_recovered_after_restart": recovered_states == replay_states,
        "duplicate_event_effect_once": processed_count == 1 and feature_count == 3,
        "future_data_ignored": bool(invariants["future_data_ignored"]),
        "forbidden_patch_rejected": bool(invariants["forbidden_patch_rejected"]),
        "production_broker_disabled": not bool(invariants["real_broker_enabled"]),
        "real_llm_disabled": not bool(invariants["real_llm_enabled"]),
    }
    return VerificationReport(
        run_id=run_id,
        passed=all(checks.values()),
        checks=checks,
        original_hash=original_hash,
        replay_hash=replay.result_hash,
    )


def verify_ledger_arm(
    session_factory: sessionmaker[Session],
    arm_id: str,
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    with session_factory() as session:
        if run_id is not None and session.get(RunRow, run_id) is None:
            raise ValueError(f"Unknown run: {run_id}")
        statement = (
            select(
                LedgerTransactionRow.ledger_transaction_id,
                func.sum(LedgerPostingRow.usd_value_delta),
            )
            .join(
                LedgerPostingRow,
                LedgerPostingRow.ledger_transaction_id
                == LedgerTransactionRow.ledger_transaction_id,
            )
            .where(LedgerTransactionRow.arm_id == arm_id)
            .group_by(LedgerTransactionRow.ledger_transaction_id)
        )
        if run_id is not None:
            statement = statement.where(LedgerTransactionRow.run_id == run_id)
        balances = list(session.execute(statement))
    unbalanced = [
        transaction_id
        for transaction_id, balance in balances
        if abs(Decimal(balance)) > Decimal("0.000001")
    ]
    return {
        "run_id": run_id,
        "arm_id": arm_id,
        "transaction_count": len(balances),
        "balanced": not unbalanced,
        "unbalanced_transaction_ids": unbalanced,
    }


def _ledger_balanced(session: Session, run_id: str) -> bool:
    statement = (
        select(
            LedgerTransactionRow.ledger_transaction_id,
            func.sum(LedgerPostingRow.usd_value_delta),
        )
        .join(
            LedgerPostingRow,
            LedgerPostingRow.ledger_transaction_id
            == LedgerTransactionRow.ledger_transaction_id,
        )
        .where(LedgerTransactionRow.run_id == run_id)
        .group_by(LedgerTransactionRow.ledger_transaction_id)
    )
    return all(
        abs(Decimal(balance)) <= Decimal("0.000001")
        for _, balance in session.execute(statement)
    )
