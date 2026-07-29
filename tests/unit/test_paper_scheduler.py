from __future__ import annotations

from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

import trading.runtime.scheduler as scheduler_module
from trading.data.alpaca_reference import MarketSession
from trading.persistence.models import RunRow
from trading.runtime.forward_paper import ForwardPaperConflict
from trading.runtime.paper_worker import PaperRuntimeWorker
from trading.runtime.scheduler import (
    PaperCycleFenceError,
    PaperCycleSlot,
    PaperCycleStore,
    build_session_slots,
)
from trading.settings import ConfigBundle


def test_same_time_cycles_run_news_before_decision(config_bundle) -> None:
    documents = deepcopy(config_bundle.documents)
    documents["schedules.yaml"]["news_poll_minutes"] = 30
    half_hour_config = ConfigBundle(
        documents=documents,
        manifest_hash=config_bundle.manifest_hash,
    )
    market_session = MarketSession(
        session_date=date(2026, 7, 27),
        open_at=datetime(2026, 7, 27, 13, 30, tzinfo=UTC),
        close_at=datetime(2026, 7, 27, 20, 0, tzinfo=UTC),
        payload_hash="a" * 64,
        available_at=datetime(2026, 7, 26, 12, 0, tzinfo=UTC),
        raw_object_uri="raw://calendar",
    )
    decision_time = datetime(2026, 7, 27, 18, 0, tzinfo=UTC)

    kinds = [
        slot.cycle_kind
        for slot in build_session_slots(market_session, config=half_hour_config)
        if slot.scheduled_at == decision_time
    ]

    assert kinds == ["NAV", "NEWS", "DECISION", "EXECUTION"]


def test_same_time_late_nav_runs_loss_guard_with_scheduled_cutoff() -> None:
    scheduled_at = datetime(2026, 7, 27, 18, 0, tzinfo=UTC)
    actual = scheduled_at + timedelta(seconds=9)
    captured: dict[str, object] = {}

    class Paper:
        @staticmethod
        def record_nav(**_kwargs):
            return [SimpleNamespace(nav_snapshot_id="nav-1")]

    class Forward:
        @staticmethod
        def decide(_cycle, **kwargs):
            captured.update(kwargs)
            return {"status": "NO_B3_LOSS_TRIGGER"}

    class Clock:
        @staticmethod
        def now() -> datetime:
            return actual

    worker = PaperRuntimeWorker.__new__(PaperRuntimeWorker)
    worker._paper = Paper()
    worker._forward_trading = Forward()
    worker._settings = SimpleNamespace(
        paper_run_id="paper-test",
        market_quote_stale_seconds=15,
    )
    worker._clock = Clock()
    cycle = SimpleNamespace(
        cycle_kind="NAV",
        cycle_id="nav-cycle",
        scheduled_at=scheduled_at,
    )

    worker._process_cycle(cycle, {}, actual)

    assert captured["data_available_cutoff"] == scheduled_at
    assert captured["created_at"] == actual
    assert captured["loss_trigger_only"] is True


def test_forward_conflict_defers_cycle_instead_of_failing(monkeypatch) -> None:
    scheduled_at = datetime(2026, 7, 27, 18, 0, tzinfo=UTC)
    actual = scheduled_at + timedelta(seconds=3)
    cycle = SimpleNamespace(
        cycle_kind="EXECUTION",
        cycle_id="execution-conflict",
        scheduled_at=scheduled_at,
        lease_owner="worker-1",
        attempt_count=2,
    )

    class Cycles:
        def __init__(self) -> None:
            self.deferred: dict[str, object] | None = None

        @staticmethod
        def claim_next(**_kwargs):
            return cycle

        @staticmethod
        def heartbeat(**_kwargs) -> None:
            pass

        def defer(self, cycle_id: str, **kwargs) -> None:
            self.deferred = {"cycle_id": cycle_id, **kwargs}

        @staticmethod
        def fail(*_args, **_kwargs) -> None:
            raise AssertionError("Forward conflicts must not terminally fail the cycle")

    class Clock:
        @staticmethod
        def now() -> datetime:
            return actual

    cycles = Cycles()
    worker = PaperRuntimeWorker.__new__(PaperRuntimeWorker)
    worker._cycles = cycles
    worker._paper = SimpleNamespace()
    worker._settings = SimpleNamespace(paper_run_id="paper-test")
    worker._config = SimpleNamespace(manifest_hash="a" * 64)
    worker._clock = Clock()
    worker._grace = timedelta(minutes=5)

    def conflict(*_args, **_kwargs):
        raise ForwardPaperConflict("arm state changed")

    monkeypatch.setattr(worker, "_process_cycle", conflict)

    result = worker.tick(now=actual)

    assert result == {
        "processed": False,
        "deferred": True,
        "cycle_id": cycle.cycle_id,
        "detail": "arm state changed",
    }
    assert cycles.deferred is not None
    assert cycles.deferred["cycle_id"] == cycle.cycle_id
    assert cycles.deferred["lease_owner"] == "worker-1"
    assert cycles.deferred["attempt_count"] == 2
    assert cycles.deferred["code"] == "FORWARD_CONFLICT_RETRY"


def test_reclaimed_cycle_rejects_stale_worker_completion(sqlite_database) -> None:
    _, _, factory = sqlite_database
    scheduled_at = datetime(2026, 7, 27, 14, 0, tzinfo=UTC)
    with factory.begin() as session:
        session.add(
            RunRow(
                run_id="paper-fence",
                mode="PAPER",
                experiment_version="test",
                config_manifest_hash="a" * 64,
                code_commit="test",
                started_at=scheduled_at,
                ended_at=None,
                status="RUNNING",
                result_manifest={},
                result_hash=None,
            )
        )
    store = PaperCycleStore(factory)
    store.ensure_slots(
        run_id="paper-fence",
        slots=(PaperCycleSlot("NAV", scheduled_at),),
        now=scheduled_at,
    )
    first = store.claim_next(
        run_id="paper-fence",
        now=scheduled_at,
        grace=timedelta(minutes=5),
        lease=timedelta(minutes=1),
        owner="worker-old",
    )
    assert first is not None
    second = store.claim_next(
        run_id="paper-fence",
        now=scheduled_at + timedelta(minutes=2),
        grace=timedelta(minutes=5),
        lease=timedelta(minutes=5),
        owner="worker-new",
    )
    assert second is not None

    with pytest.raises(PaperCycleFenceError, match="no longer owned"):
        store.complete(
            first.cycle_id,
            lease_owner="worker-old",
            attempt_count=first.attempt_count,
            cutoff=scheduled_at,
            input_manifest={"attempt": "old"},
            output_manifest={"result": "old"},
            now=scheduled_at + timedelta(minutes=2),
        )

    store.complete(
        second.cycle_id,
        lease_owner="worker-new",
        attempt_count=second.attempt_count,
        cutoff=scheduled_at + timedelta(minutes=2),
        input_manifest={"attempt": "new"},
        output_manifest={"result": "new"},
        now=scheduled_at + timedelta(minutes=3),
    )


def test_claim_uses_authoritative_database_clock(
    monkeypatch,
    sqlite_database,
) -> None:
    _, _, factory = sqlite_database
    base = datetime(2026, 7, 29, 13, 30, tzinfo=UTC)
    scheduled_at = base + timedelta(seconds=1)
    authority = {"now": base}
    with factory.begin() as session:
        session.add(
            RunRow(
                run_id="paper-database-clock",
                mode="PAPER",
                experiment_version="test",
                config_manifest_hash="a" * 64,
                code_commit="test",
                started_at=base,
                ended_at=None,
                status="RUNNING",
                result_manifest={},
                result_hash=None,
            )
        )
    store = PaperCycleStore(factory)
    store.ensure_slots(
        run_id="paper-database-clock",
        slots=(PaperCycleSlot("NAV", scheduled_at),),
        now=base,
    )
    monkeypatch.setattr(
        scheduler_module,
        "_lease_check_now",
        lambda _session, _fallback: authority["now"],
    )

    # A fast host must not claim before the database clock reaches the slot.
    assert (
        store.claim_next(
            run_id="paper-database-clock",
            now=base + timedelta(seconds=5),
            grace=timedelta(minutes=5),
            owner="worker-fast-host",
        )
        is None
    )

    # Once the database clock is due, a slow host may claim. Stored lease
    # timestamps must use the same authoritative instant.
    authority["now"] = base + timedelta(seconds=2)
    claimed = store.claim_next(
        run_id="paper-database-clock",
        now=base,
        grace=timedelta(minutes=5),
        lease=timedelta(minutes=1),
        owner="worker-slow-host",
    )

    assert claimed is not None
    assert claimed.started_at is not None
    assert claimed.lease_expires_at is not None
    assert claimed.started_at.replace(tzinfo=UTC) == authority["now"]
    assert claimed.lease_expires_at.replace(tzinfo=UTC) == (
        authority["now"] + timedelta(minutes=1)
    )
