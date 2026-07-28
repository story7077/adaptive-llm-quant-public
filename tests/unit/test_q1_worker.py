from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from trading.domain.algorithm import Q1_ALGORITHM_VERSION
from trading.persistence.q1_runtime import Q1StaleWorkerError
from trading.runtime.q1_config import operational_config
from trading.runtime.q1_cycle import Q1CycleNotReady
from trading.runtime.q1_scheduler import Q1_CYCLE_KINDS
from trading.runtime.q1_worker import NEW_YORK, Q1PaperRuntimeWorker
from trading.settings import Q1ConfigBundle, Settings, load_q1_config_bundle


def _cycle() -> SimpleNamespace:
    return SimpleNamespace(
        cycle_id="q1-cycle-1",
        cycle_kind="Q1_NAV_RISK",
        scheduled_at=datetime(2026, 7, 27, 13, 45, tzinfo=UTC),
        lease_owner="q1-worker",
        attempt_count=2,
    )


def _worker(*, cycles: object, processor: object) -> Q1PaperRuntimeWorker:
    worker = Q1PaperRuntimeWorker.__new__(Q1PaperRuntimeWorker)
    worker._settings = SimpleNamespace(paper_run_id="q1-run")
    worker._paper = SimpleNamespace(
        status=lambda _run_id: {"state": "RUNNING"}
    )
    worker._cycles = cycles
    worker._processor = processor
    worker._grace = timedelta(minutes=15)
    return worker


def test_q1_worker_claims_only_q1_slots_and_does_not_double_complete() -> None:
    cycle = _cycle()

    class Cycles:
        def __init__(self) -> None:
            self.claimed_kinds: frozenset[str] | None = None

        def claim_next(self, **kwargs):
            self.claimed_kinds = kwargs["kinds"]
            return cycle

        @staticmethod
        def heartbeat(**_kwargs) -> None:
            pass

        @staticmethod
        def complete(**_kwargs) -> None:
            raise AssertionError("Q1 worker must not complete processor-owned cycles")

    class Processor:
        @staticmethod
        def process(_cycle):
            return {"status": "COMMITTED_IN_FENCED_TRANSACTION"}

    cycles = Cycles()
    result = _worker(cycles=cycles, processor=Processor()).tick(
        now=cycle.scheduled_at
    )

    assert cycles.claimed_kinds == Q1_CYCLE_KINDS
    assert result["processed"] is True
    assert result["output"]["status"] == "COMMITTED_IN_FENCED_TRANSACTION"


def test_q1_worker_defers_data_not_ready_with_the_claimed_fence() -> None:
    cycle = _cycle()

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

    class Processor:
        @staticmethod
        def process(_cycle):
            raise Q1CycleNotReady("fresh quote missing")

    cycles = Cycles()
    result = _worker(cycles=cycles, processor=Processor()).tick(
        now=cycle.scheduled_at
    )

    assert result["deferred"] is True
    assert cycles.deferred is not None
    assert cycles.deferred["lease_owner"] == cycle.lease_owner
    assert cycles.deferred["attempt_count"] == cycle.attempt_count
    assert cycles.deferred["code"] == "Q1_DATA_NOT_READY"


def test_q1_worker_never_terminally_writes_after_stale_processor_fence() -> None:
    cycle = _cycle()

    class Cycles:
        @staticmethod
        def claim_next(**_kwargs):
            return cycle

        @staticmethod
        def heartbeat(**_kwargs) -> None:
            pass

        @staticmethod
        def fail(*_args, **_kwargs) -> None:
            raise AssertionError("A stale Q1 worker must not append terminal state")

    class Processor:
        @staticmethod
        def process(_cycle):
            raise Q1StaleWorkerError("cycle reclaimed")

    result = _worker(cycles=Cycles(), processor=Processor()).tick(
        now=cycle.scheduled_at
    )

    assert result == {
        "processed": False,
        "stale_worker": True,
        "cycle_id": cycle.cycle_id,
    }


def test_q1_worker_uses_versioned_calendar_operational_ranges(
    repository_root,
    tmp_path,
) -> None:
    config = load_q1_config_bundle(repository_root / "config")
    operations = operational_config(config)
    now = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)

    class Clock:
        @staticmethod
        def now() -> datetime:
            return now

    class Reference:
        def __init__(self) -> None:
            self.start = None
            self.end = None

        async def fetch_calendar(self, *, start, end):
            self.start = start
            self.end = end
            return []

    reference = Reference()
    settings = Settings(
        database_url="sqlite+pysqlite://",
        config_dir=repository_root / "config",
        raw_store=tmp_path / "raw",
        real_broker_enabled=False,
        real_llm_enabled=False,
        production_unlock=False,
        paper_algorithm_version=Q1_ALGORITHM_VERSION,
    )
    worker = Q1PaperRuntimeWorker(
        settings=settings,
        config=config,
        paper=SimpleNamespace(
            schedule=SimpleNamespace(nav_interval_minutes=15)
        ),
        cycles=SimpleNamespace(),
        processor=SimpleNamespace(),
        reference_client=reference,
        clock=Clock(),
    )

    created = asyncio.run(worker.sync_calendar())

    local_date = now.astimezone(NEW_YORK).date()
    assert created == 0
    assert reference.start == (
        local_date
        - timedelta(days=operations.calendar_history_lookback_days)
    )
    assert reference.end == (
        local_date + timedelta(days=operations.calendar_forward_days)
    )
    assert worker._calendar_sync_interval == timedelta(
        hours=operations.calendar_sync_interval_hours
    )
    assert operations.calendar_history_lookback_days == 260


def test_q1_operational_config_rejects_insufficient_calendar_history(
    repository_root,
) -> None:
    config = load_q1_config_bundle(repository_root / "config")
    document = deepcopy(config.document)
    document["operations"]["calendar_history_lookback_days"] = 120
    invalid = Q1ConfigBundle(
        document=document,
        cost_document=config.cost_document,
        manifest_hash=config.manifest_hash,
    )

    with pytest.raises(ValueError, match="minimum completed"):
        operational_config(invalid)
