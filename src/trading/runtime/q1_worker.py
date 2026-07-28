from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from trading.data.alpaca_reference import AlpacaReferenceClient
from trading.domain.algorithm import Q1_ALGORITHM_VERSION
from trading.domain.time import Clock, SystemClock
from trading.persistence.models import PaperCycleRow
from trading.persistence.q1_runtime import Q1StaleWorkerError
from trading.runtime.q1_alpaca_paper import Q1AlpacaPaperCanaryService
from trading.runtime.q1_config import operational_config
from trading.runtime.q1_cycle import Q1CycleNotReady, Q1PaperCycleProcessor
from trading.runtime.q1_paper import Q1PaperRuntimeService
from trading.runtime.q1_scheduler import (
    Q1_CYCLE_KINDS,
    VersionedMarketSession,
    build_q1_session_slots,
)
from trading.runtime.scheduler import (
    PaperCycleFenceError,
    PaperCycleStore,
)
from trading.settings import Q1ConfigBundle, Settings

NEW_YORK = ZoneInfo("America/New_York")


class Q1WorkerConfigurationError(RuntimeError):
    pass


class Q1PaperRuntimeWorker:
    """Claims only q1 slots and delegates each atomic commit to the q1 processor."""

    def __init__(
        self,
        *,
        settings: Settings,
        config: Q1ConfigBundle,
        paper: Q1PaperRuntimeService,
        cycles: PaperCycleStore,
        processor: Q1PaperCycleProcessor,
        reference_client: AlpacaReferenceClient,
        alpaca_paper_canary: Q1AlpacaPaperCanaryService | None = None,
        clock: Clock | None = None,
    ) -> None:
        if settings.paper_algorithm_version != Q1_ALGORITHM_VERSION:
            raise Q1WorkerConfigurationError(
                "Q1 worker requires paper_algorithm_version=q1_math_core_v1"
            )
        if settings.real_broker_enabled or config.document.get(
            "real_order_routing"
        ) is not False:
            raise Q1WorkerConfigurationError(
                "Q1 worker requires real_order_routing=false"
            )
        if settings.q1_alpaca_paper_enabled and alpaca_paper_canary is None:
            raise Q1WorkerConfigurationError(
                "Enabled Alpaca Paper canary requires a Paper-only service"
            )
        if (
            not settings.q1_alpaca_paper_enabled
            and alpaca_paper_canary is not None
        ):
            raise Q1WorkerConfigurationError(
                "Alpaca Paper service requires its explicit environment gate"
            )
        self._settings = settings
        self._config = config
        self._paper = paper
        self._cycles = cycles
        self._processor = processor
        self._reference = reference_client
        self._alpaca_paper_canary = alpaca_paper_canary
        self._clock = clock or SystemClock()
        operations = operational_config(config)
        self._calendar_sync_interval = timedelta(
            hours=operations.calendar_sync_interval_hours
        )
        self._calendar_history_lookback = timedelta(
            days=operations.calendar_history_lookback_days
        )
        self._calendar_forward = timedelta(
            days=operations.calendar_forward_days
        )
        self._grace = timedelta(
            minutes=self._paper.schedule.nav_interval_minutes
        )
        self._last_calendar_sync_at: datetime | None = None

    def initialize(self) -> None:
        self._paper.initialize(
            run_id=self._settings.paper_run_id,
            account_file=self._account_file,
        )

    async def sync_calendar(self) -> int:
        """Persist calendar history, but schedule only open or future sessions."""

        now = self._clock.now()
        local_date = now.astimezone(NEW_YORK).date()
        source_sessions = await self._reference.fetch_calendar(
            start=local_date - self._calendar_history_lookback,
            end=local_date + self._calendar_forward,
        )
        created = 0
        calendar_version = str(
            self._config.document["market_calendar_version"]
        )
        for source_session in source_sessions:
            current = await asyncio.to_thread(
                self._paper.calendar_session,
                session_date=source_session.session_date,
                cutoff=now,
            )
            candidate = VersionedMarketSession.from_reference(
                source_session,
                calendar_version=calendar_version,
            )
            if (
                current is None
                or current.open_at != candidate.open_at
                or current.close_at != candidate.close_at
            ):
                await asyncio.to_thread(
                    self._paper.register_calendar_session,
                    candidate,
                    now=now,
                )
                session = candidate
            else:
                session = current
            # Historical and already-open sessions are required for signal and
            # settlement PIT lookups, but their runtime slots must never be
            # backfilled. A Q1_BOOTSTRAP scheduled before this observation can
            # never satisfy available_at <= scheduled_at and, because it is
            # retryable, would starve every later cycle forever. A run first
            # started intraday therefore begins on the next observed session.
            if session.open_at < now:
                continue
            slots = build_q1_session_slots(
                session,
                schedule=self._paper.schedule,
            )
            created += await asyncio.to_thread(
                self._cycles.ensure_slots,
                run_id=self._settings.paper_run_id,
                slots=slots,
                now=now,
            )
        self._last_calendar_sync_at = now
        return created

    def tick(self, *, now: datetime | None = None) -> dict[str, Any]:
        instant = self._clock.now() if now is None else now
        cycle = self._cycles.claim_next(
            run_id=self._settings.paper_run_id,
            now=instant,
            grace=self._grace,
            kinds=Q1_CYCLE_KINDS,
        )
        if cycle is None:
            state = str(
                self._paper.status(self._settings.paper_run_id)["state"]
            )
            self._cycles.heartbeat(
                run_id=self._settings.paper_run_id,
                state=state,
                now=instant,
            )
            return {"processed": False, "state": state}

        self._cycles.heartbeat(
            run_id=self._settings.paper_run_id,
            state="PROCESSING",
            now=instant,
            current_cycle_id=cycle.cycle_id,
        )
        try:
            output = self._processor.process(cycle)
        except Q1CycleNotReady as exc:
            return self._defer_not_ready(cycle, exc, instant)
        except Q1StaleWorkerError:
            return {
                "processed": False,
                "stale_worker": True,
                "cycle_id": cycle.cycle_id,
            }
        except Exception as exc:
            return self._fail_cycle(cycle, exc, instant)

        # Q1PaperCycleProcessor has already completed this cycle inside the
        # same fenced transaction as its immutable domain writes.
        state = str(self._paper.status(self._settings.paper_run_id)["state"])
        self._cycles.heartbeat(
            run_id=self._settings.paper_run_id,
            state=state,
            now=instant,
        )
        return {
            "processed": True,
            "cycle_id": cycle.cycle_id,
            "kind": cycle.cycle_kind,
            "output": output,
        }

    async def sync_alpaca_paper(
        self,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if self._alpaca_paper_canary is None:
            return {
                "enabled": False,
                "state": "DISABLED_NOT_CONFIGURED",
                "real_order_routing": False,
            }
        instant = self._clock.now() if now is None else now
        return await self._alpaca_paper_canary.sync(
            self._settings.paper_run_id,
            now=instant,
        )

    async def run_forever(
        self,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        stop = stop_event or asyncio.Event()
        self.initialize()
        try:
            await self.sync_calendar()
            while not stop.is_set():
                now = self._clock.now()
                try:
                    if (
                        self._last_calendar_sync_at is None
                        or now - self._last_calendar_sync_at
                        >= self._calendar_sync_interval
                    ):
                        await self.sync_calendar()
                    await asyncio.to_thread(self.tick, now=now)
                    if self._alpaca_paper_canary is not None:
                        try:
                            await self.sync_alpaca_paper(now=now)
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            await asyncio.to_thread(
                                self._cycles.heartbeat,
                                run_id=self._settings.paper_run_id,
                                state="DEGRADED_ALPACA_PAPER_CANARY",
                                now=self._clock.now(),
                                error_code=type(exc).__name__.upper(),
                                error_detail=(
                                    "Alpaca Paper canary sync failed; "
                                    "the deterministic Q1 lane remains active"
                                ),
                            )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await asyncio.to_thread(
                        self._cycles.heartbeat,
                        run_id=self._settings.paper_run_id,
                        state="DEGRADED",
                        now=self._clock.now(),
                        error_code=type(exc).__name__.upper(),
                        error_detail=str(exc),
                    )
                with suppress(TimeoutError):
                    await asyncio.wait_for(
                        stop.wait(),
                        timeout=self._settings.paper_poll_seconds,
                    )
        finally:
            await self._reference.aclose()
            if self._alpaca_paper_canary is not None:
                await self._alpaca_paper_canary.aclose()
            await asyncio.to_thread(
                self._cycles.heartbeat,
                run_id=self._settings.paper_run_id,
                state="STOPPED",
                now=self._clock.now(),
            )

    def _defer_not_ready(
        self,
        cycle: PaperCycleRow,
        exc: Q1CycleNotReady,
        now: datetime,
    ) -> dict[str, Any]:
        try:
            self._cycles.defer(
                cycle.cycle_id,
                lease_owner=_lease_owner(cycle),
                attempt_count=cycle.attempt_count,
                code="Q1_DATA_NOT_READY",
                detail=str(exc),
                now=now,
            )
        except PaperCycleFenceError:
            return {
                "processed": False,
                "stale_worker": True,
                "cycle_id": cycle.cycle_id,
            }
        return {
            "processed": False,
            "deferred": True,
            "cycle_id": cycle.cycle_id,
            "detail": str(exc),
        }

    def _fail_cycle(
        self,
        cycle: PaperCycleRow,
        exc: Exception,
        now: datetime,
    ) -> dict[str, Any]:
        try:
            self._cycles.fail(
                cycle.cycle_id,
                lease_owner=_lease_owner(cycle),
                attempt_count=cycle.attempt_count,
                code=type(exc).__name__.upper(),
                detail=str(exc),
                now=now,
            )
        except PaperCycleFenceError:
            return {
                "processed": False,
                "stale_worker": True,
                "cycle_id": cycle.cycle_id,
            }
        self._cycles.heartbeat(
            run_id=self._settings.paper_run_id,
            state="DEGRADED",
            now=now,
            error_code=type(exc).__name__.upper(),
            error_detail=str(exc),
        )
        return {
            "processed": False,
            "failed": True,
            "cycle_id": cycle.cycle_id,
            "detail": str(exc),
        }

    @property
    def _account_file(self) -> Path:
        if self._settings.paper_account_file is None:
            raise Q1WorkerConfigurationError("A paper account file is required")
        return self._settings.paper_account_file


def _lease_owner(cycle: PaperCycleRow) -> str:
    if cycle.lease_owner is None:
        raise Q1WorkerConfigurationError("Claimed Q1 cycle has no lease owner")
    return cycle.lease_owner
