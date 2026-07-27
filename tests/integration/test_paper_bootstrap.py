from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from trading.persistence.models import (
    PaperAccountSpecRow,
    PaperBootstrapCompletionRow,
    PaperBootstrapMarkRow,
    PaperCashBalanceRow,
    PaperCycleRow,
    PaperPositionRow,
    PaperRuntimeStatusRow,
    RunRow,
)
from trading.persistence.paper import (
    PaperBootstrapConflict,
    PaperBootstrapError,
    PaperBootstrapService,
    load_paper_account_spec,
)


def test_snapshot_config_contains_only_synthetic_example_values(
    repository_root: Path,
) -> None:
    spec = load_paper_account_spec(
        repository_root / "config" / "paper-account.example.yaml"
    )

    cash = {item.currency: item for item in spec.cash}
    assert cash["USD"].amount == Decimal("100000.00")
    assert cash["USD"].tradable is True
    assert cash["KRW"].amount == Decimal("0")
    assert cash["KRW"].tradable is False
    assert cash["KRW"].exclusion_reason == "SYNTHETIC_NON_TRADABLE_EXAMPLE"

    positions = {item.symbol: item.quantity for item in spec.positions}
    assert positions == {
        "SPY": Decimal("11"),
        "QQQ": Decimal("7"),
        "IWM": Decimal("13"),
        "SMH": Decimal("9"),
        "TLT": Decimal("17"),
        "HYG": Decimal("19"),
        "GLD": Decimal("6"),
    }


def test_account_provision_is_idempotent_and_append_only(
    repository_root: Path,
    sqlite_database,
) -> None:
    _, engine, factory = sqlite_database
    service = PaperBootstrapService(factory)
    created_at = datetime(2026, 7, 27, 1, 0, tzinfo=UTC)

    first = service.provision_from_file(
        repository_root / "config" / "paper-account.example.yaml",
        now=created_at,
    )
    second = service.provision(first.spec, now=created_at + timedelta(minutes=1))

    assert first.created is True
    assert second.created is False
    assert second.account_spec_id == first.account_spec_id
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(PaperAccountSpecRow)) == 1
        assert session.scalar(select(func.count()).select_from(PaperCashBalanceRow)) == 2
        assert session.scalar(select(func.count()).select_from(PaperPositionRow)) == 7
        gld = session.scalar(
            select(PaperPositionRow).where(PaperPositionRow.symbol == "GLD")
        )
        assert gld is not None
        assert gld.quantity == Decimal("6.0000000000")

    with (
        engine.connect() as connection,
        connection.begin(),
        pytest.raises(DBAPIError, match="append-only"),
    ):
        connection.execute(
            text(
                "UPDATE paper_positions SET quantity=1 "
                "WHERE symbol='GLD'"
            )
        )


def test_bootstrap_requires_common_complete_marks_and_is_idempotent(
    repository_root: Path,
    sqlite_database,
) -> None:
    _, _, factory = sqlite_database
    service = PaperBootstrapService(factory)
    started_at = datetime(2026, 7, 27, 13, 20, tzinfo=UTC)
    account = service.provision_from_file(
        repository_root / "config" / "paper-account.example.yaml",
        now=started_at,
    )
    _insert_paper_run(factory, "forward-paper", started_at)
    marked_at = started_at + timedelta(minutes=11)
    completed_at = marked_at + timedelta(seconds=2)
    prices = _prices()

    first = service.complete(
        run_id="forward-paper",
        account_spec_id=account.account_spec_id,
        prices=prices,
        common_mark_at=marked_at,
        source_kind="ALPACA_IEX_QUOTE",
        now=completed_at,
    )
    replay = service.complete(
        run_id="forward-paper",
        account_spec_id=account.account_spec_id,
        prices=prices,
        common_mark_at=marked_at,
        source_kind="ALPACA_IEX_QUOTE",
        now=completed_at + timedelta(minutes=1),
    )

    expected_nav = (
        Decimal("100000.00")
        + sum(
            (
                position.quantity * prices[position.symbol]
                for position in account.spec.positions
            ),
            Decimal("0"),
        )
    ).quantize(Decimal("0.0000000001"))
    assert first.created is True
    assert replay.created is False
    assert replay.completion == first.completion
    assert first.completion.initial_nav_usd == expected_nav

    with factory() as session:
        assert session.scalar(
            select(func.count()).select_from(PaperBootstrapMarkRow)
        ) == 7
        assert session.scalar(
            select(func.count()).select_from(PaperBootstrapCompletionRow)
        ) == 1
        marked_times = set(
            session.scalars(select(PaperBootstrapMarkRow.marked_at))
        )
        assert len(marked_times) == 1

    with pytest.raises(PaperBootstrapError, match="exactly cover positions"):
        service.complete(
            run_id="forward-paper",
            account_spec_id=account.account_spec_id,
            prices={key: value for key, value in prices.items() if key != "GLD"},
            common_mark_at=marked_at,
            source_kind="ALPACA_IEX_QUOTE",
            now=completed_at,
        )

    changed = dict(prices)
    changed["GLD"] = Decimal("320.01")
    with pytest.raises(PaperBootstrapConflict, match="different bootstrap"):
        service.complete(
            run_id="forward-paper",
            account_spec_id=account.account_spec_id,
            prices=changed,
            common_mark_at=marked_at,
            source_kind="ALPACA_IEX_QUOTE",
            now=completed_at,
        )


def test_runtime_cycle_and_status_are_mutable_projections(sqlite_database) -> None:
    _, _, factory = sqlite_database
    now = datetime(2026, 7, 27, 13, 20, tzinfo=UTC)
    _insert_paper_run(factory, "runtime-paper", now)

    with factory.begin() as session:
        session.add(
            PaperCycleRow(
                cycle_id="cycle-1",
                run_id="runtime-paper",
                cycle_kind="MARKET_OPEN",
                scheduled_at=now,
                data_available_cutoff=None,
                status="PENDING",
                idempotency_key="runtime-paper:MARKET_OPEN:2026-07-27T13:20:00Z",
                lease_owner=None,
                lease_expires_at=None,
                attempt_count=0,
                input_manifest_hash=None,
                output_manifest_hash=None,
                started_at=None,
                completed_at=None,
                last_error_code=None,
                last_error_detail=None,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            PaperRuntimeStatusRow(
                run_id="runtime-paper",
                state="STARTING",
                current_cycle_id=None,
                heartbeat_at=None,
                last_completed_cycle_at=None,
                last_error_code=None,
                last_error_detail=None,
                process_id=1234,
                updated_at=now,
            )
        )

    with factory.begin() as session:
        cycle = session.get(PaperCycleRow, "cycle-1")
        status = session.get(PaperRuntimeStatusRow, "runtime-paper")
        assert cycle is not None
        assert status is not None
        cycle.status = "COMPLETED"
        cycle.completed_at = now + timedelta(seconds=1)
        cycle.updated_at = now + timedelta(seconds=1)
        status.state = "RUNNING"
        status.current_cycle_id = cycle.cycle_id
        status.heartbeat_at = now + timedelta(seconds=1)
        status.updated_at = now + timedelta(seconds=1)

    with factory() as session:
        assert session.get(PaperCycleRow, "cycle-1").status == "COMPLETED"  # type: ignore[union-attr]
        assert session.get(PaperRuntimeStatusRow, "runtime-paper").state == "RUNNING"  # type: ignore[union-attr]

    with pytest.raises(IntegrityError), factory.begin() as session:
        session.add(
            PaperCycleRow(
                cycle_id="cycle-duplicate",
                run_id="runtime-paper",
                cycle_kind="MARKET_OPEN",
                scheduled_at=now,
                data_available_cutoff=None,
                status="PENDING",
                idempotency_key="another-key",
                lease_owner=None,
                lease_expires_at=None,
                attempt_count=0,
                input_manifest_hash=None,
                output_manifest_hash=None,
                started_at=None,
                completed_at=None,
                last_error_code=None,
                last_error_detail=None,
                created_at=now,
                updated_at=now,
            )
        )


def _insert_paper_run(factory, run_id: str, started_at: datetime) -> None:
    with factory.begin() as session:
        session.add(
            RunRow(
                run_id=run_id,
                mode="PAPER",
                experiment_version="phase1-paper",
                config_manifest_hash="0" * 64,
                code_commit="working-tree",
                started_at=started_at,
                ended_at=None,
                status="RUNNING",
                result_manifest=None,
                result_hash=None,
            )
        )


def _prices() -> dict[str, Decimal]:
    return {
        "SPY": Decimal("650.25"),
        "QQQ": Decimal("560.45"),
        "IWM": Decimal("240.10"),
        "SMH": Decimal("310.25"),
        "TLT": Decimal("90.50"),
        "HYG": Decimal("80.75"),
        "GLD": Decimal("320.00"),
    }
