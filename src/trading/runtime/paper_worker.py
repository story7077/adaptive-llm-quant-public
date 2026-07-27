from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from trading.data.alpaca_reference import AlpacaReferenceClient
from trading.domain.time import Clock, SystemClock
from trading.persistence.models import PaperCycleRow
from trading.runtime.forward_paper import (
    ForwardPaperConflict,
    ForwardPaperTradingService,
)
from trading.runtime.paper import (
    PaperBootstrapNotReady,
    PaperRuntimeError,
    PaperRuntimeService,
)
from trading.runtime.scheduler import PaperCycleStore, build_session_slots
from trading.settings import ConfigBundle, Settings

type CycleRunner = Callable[[PaperCycleRow, dict[str, Any]], Awaitable[dict[str, Any]]]
NEW_YORK = ZoneInfo("America/New_York")
CORE_CYCLE_KINDS = frozenset(
    {"BOOTSTRAP", "NAV", "EXECUTION", "DAILY_REPORT", "RECONCILIATION"}
)
AI_CYCLE_KINDS = frozenset({"NEWS", "DECISION"})


class PaperRuntimeWorker:
    def __init__(
        self,
        *,
        settings: Settings,
        config: ConfigBundle,
        paper: PaperRuntimeService,
        cycles: PaperCycleStore,
        reference_client: AlpacaReferenceClient,
        news_runner: CycleRunner | None = None,
        commander_runner: CycleRunner | None = None,
        forward_trading: ForwardPaperTradingService | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._settings = settings
        self._config = config
        self._paper = paper
        self._cycles = cycles
        self._reference = reference_client
        self._news_runner = news_runner
        self._commander_runner = commander_runner
        self._forward_trading = forward_trading
        self._clock = clock or SystemClock()
        schedules = config.get("schedules.yaml")
        self._grace = timedelta(minutes=int(schedules["cycle_grace_minutes"]))
        self._bootstrap_timeout = timedelta(
            minutes=int(schedules["bootstrap_quote_timeout_minutes"])
        )
        self._baseline_rebalance_clock = str(
            config.get("forward-paper.yaml")["baseline_contract"][
                "rebalance_time_et"
            ]
        )
        self._last_calendar_sync_at = None

    async def run_forever(self, stop_event: asyncio.Event | None = None) -> None:
        stop = stop_event or asyncio.Event()
        self.initialize()
        try:
            await self.sync_calendar()
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(
                    self._run_lane(
                        stop,
                        kinds=CORE_CYCLE_KINDS,
                        sync_calendar=True,
                    ),
                    name="paper-core-lane",
                )
                tasks.create_task(
                    self._run_lane(
                        stop,
                        kinds=AI_CYCLE_KINDS,
                        sync_calendar=False,
                    ),
                    name="paper-ai-lane",
                )
        finally:
            await self._reference.aclose()
            await asyncio.to_thread(
                self._cycles.heartbeat,
                run_id=self._settings.paper_run_id,
                state="STOPPED",
                now=self._clock.now(),
            )

    async def _run_lane(
        self,
        stop: asyncio.Event,
        *,
        kinds: frozenset[str],
        sync_calendar: bool,
    ) -> None:
        while not stop.is_set():
            now = self._clock.now()
            try:
                if (
                    sync_calendar
                    and (
                        self._last_calendar_sync_at is None
                        or now - self._last_calendar_sync_at >= timedelta(hours=6)
                    )
                ):
                    await self.sync_calendar()
                await asyncio.to_thread(self.tick, now=now, kinds=kinds)
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

    def initialize(self) -> None:
        self._paper.initialize(
            run_id=self._settings.paper_run_id,
            account_file=self._account_file,
        )

    async def sync_calendar(self) -> int:
        now = self._clock.now()
        local_date = now.astimezone(NEW_YORK).date()
        sessions = await self._reference.fetch_calendar(
            start=local_date - timedelta(days=1),
            end=local_date + timedelta(days=21),
        )
        created = 0
        for market_session in sessions:
            created += await asyncio.to_thread(
                self._cycles.ensure_slots,
                run_id=self._settings.paper_run_id,
                slots=build_session_slots(market_session, config=self._config),
                now=now,
            )
        self._last_calendar_sync_at = now
        return created

    def tick(
        self,
        *,
        now: datetime | None = None,
        kinds: frozenset[str] | None = None,
    ) -> dict[str, Any]:
        instant = self._clock.now() if now is None else now
        cycle = self._cycles.claim_next(
            run_id=self._settings.paper_run_id,
            now=instant,
            grace=self._grace,
            kinds=kinds,
        )
        if cycle is None:
            state = self._paper.status(self._settings.paper_run_id)["state"]
            self._cycles.heartbeat(
                run_id=self._settings.paper_run_id,
                state=str(state),
                now=instant,
            )
            return {"processed": False, "state": state}

        self._cycles.heartbeat(
            run_id=self._settings.paper_run_id,
            state="PROCESSING",
            now=instant,
            current_cycle_id=cycle.cycle_id,
        )
        data_available_cutoff = (
            _aware(cycle.scheduled_at)
            if cycle.cycle_kind == "DECISION"
            else instant
        )
        base_input = {
            "cycle_id": cycle.cycle_id,
            "kind": cycle.cycle_kind,
            "scheduled_at": cycle.scheduled_at,
            "data_available_cutoff": data_available_cutoff,
            "config_manifest_hash": self._config.manifest_hash,
            "real_order_routing": False,
        }
        try:
            output = self._process_cycle(cycle, base_input, instant)
        except PaperBootstrapNotReady as exc:
            if (
                cycle.cycle_kind == "BOOTSTRAP"
                and instant <= _aware(cycle.scheduled_at) + self._bootstrap_timeout
            ):
                self._cycles.defer(
                    cycle.cycle_id,
                    lease_owner=_required_lease_owner(cycle),
                    attempt_count=cycle.attempt_count,
                    code="WAITING_FOR_COMMON_T0",
                    detail=str(exc),
                    now=self._clock.now(),
                )
                return {
                    "processed": False,
                    "deferred": True,
                    "cycle_id": cycle.cycle_id,
                    "detail": str(exc),
                }
            self._cycles.fail(
                cycle.cycle_id,
                lease_owner=_required_lease_owner(cycle),
                attempt_count=cycle.attempt_count,
                code="BOOTSTRAP_WINDOW_EXPIRED",
                detail=str(exc),
                now=self._clock.now(),
            )
            return {
                "processed": False,
                "failed": True,
                "cycle_id": cycle.cycle_id,
            }
        except ForwardPaperConflict as exc:
            self._cycles.defer(
                cycle.cycle_id,
                lease_owner=_required_lease_owner(cycle),
                attempt_count=cycle.attempt_count,
                code="FORWARD_CONFLICT_RETRY",
                detail=str(exc),
                now=self._clock.now(),
            )
            return {
                "processed": False,
                "deferred": True,
                "cycle_id": cycle.cycle_id,
                "detail": str(exc),
            }
        except Exception as exc:
            self._cycles.fail(
                cycle.cycle_id,
                lease_owner=_required_lease_owner(cycle),
                attempt_count=cycle.attempt_count,
                code=type(exc).__name__.upper(),
                detail=str(exc),
                now=self._clock.now(),
            )
            self._cycles.heartbeat(
                run_id=self._settings.paper_run_id,
                state="DEGRADED",
                now=instant,
                error_code=type(exc).__name__.upper(),
                error_detail=str(exc),
            )
            return {
                "processed": False,
                "failed": True,
                "cycle_id": cycle.cycle_id,
                "detail": str(exc),
            }

        completion_cutoff = output.get("data_available_cutoff", instant)
        if not isinstance(completion_cutoff, datetime):
            completion_cutoff = instant
        completed_at = self._clock.now()
        self._cycles.complete(
            cycle.cycle_id,
            lease_owner=_required_lease_owner(cycle),
            attempt_count=cycle.attempt_count,
            cutoff=completion_cutoff,
            input_manifest=base_input,
            output_manifest=output,
            now=completed_at,
        )
        self._cycles.heartbeat(
            run_id=self._settings.paper_run_id,
            state=str(self._paper.status(self._settings.paper_run_id)["state"]),
            now=instant,
        )
        return {
            "processed": True,
            "cycle_id": cycle.cycle_id,
            "kind": cycle.cycle_kind,
            "output": output,
        }

    def _process_cycle(
        self,
        cycle: PaperCycleRow,
        context: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        if cycle.cycle_kind == "BOOTSTRAP":
            completion = self._paper.bootstrap_from_fresh_quotes(
                run_id=self._settings.paper_run_id,
                session_open_at=cycle.scheduled_at,
                account_file=self._account_file,
                max_quote_age_seconds=self._settings.market_quote_stale_seconds,
                now=now,
            )
            return {
                "status": "T0_ESTABLISHED",
                "performance_start_at": completion.common_mark_at,
                "initial_nav_usd": completion.initial_nav_usd,
                "input_manifest_hash": completion.input_manifest_hash,
            }
        if cycle.cycle_kind == "NAV":
            snapshots = self._paper.record_nav(
                run_id=self._settings.paper_run_id,
                as_of=now,
                snapshot_scope=cycle.cycle_id,
                max_quote_age_seconds=self._settings.market_quote_stale_seconds,
            )
            risk_guard = (
                None
                if self._forward_trading is None
                else self._forward_trading.decide(
                    cycle,
                    run_id=self._settings.paper_run_id,
                    data_available_cutoff=_aware(cycle.scheduled_at),
                    created_at=self._clock.now(),
                    loss_trigger_only=True,
                )
            )
            return {
                "status": "NAV_RECORDED",
                "nav_snapshot_ids": [
                    item.nav_snapshot_id for item in snapshots
                ],
                "b3_loss_guard": risk_guard,
            }
        if cycle.cycle_kind == "NEWS":
            if self._news_runner is None:
                return {
                    "status": "NEWS_ANALYZER_DISABLED",
                    "reason": "No fail-closed WebGPT runner is configured",
                }
            return _run_async_from_worker(self._news_runner(cycle, context))
        if cycle.cycle_kind == "DECISION":
            scheduled_clock = _scheduled_clock_et(cycle.scheduled_at)
            if scheduled_clock == self._baseline_rebalance_clock:
                commander_output = {
                    "status": "NO_CHANGE",
                    "reason_code": "USES_LATEST_PRE_REBALANCE_VERSIONED_POLICY",
                    "orders_created": 0,
                }
            elif self._commander_runner is None:
                commander_output = {
                    "status": "NO_CHANGE",
                    "reason_code": "COMMANDER_NOT_CONFIGURED",
                    "orders_created": 0,
                }
            else:
                try:
                    commander_output = _run_async_from_worker(
                        self._commander_runner(cycle, context)
                    )
                except Exception as exc:
                    commander_output = {
                        "status": "ERROR",
                        "reason_code": type(exc).__name__.upper(),
                        "reason_detail": str(exc)[:500],
                        "orders_created": 0,
                    }
            if self._forward_trading is None:
                return {
                    "status": "FORWARD_TRADING_NOT_CONFIGURED",
                    "commander": commander_output,
                    "orders_created": 0,
                    "data_available_cutoff": context["data_available_cutoff"],
                }
            trading_output = self._forward_trading.decide(
                cycle,
                run_id=self._settings.paper_run_id,
                data_available_cutoff=context["data_available_cutoff"],
                created_at=self._clock.now(),
                policy_change_only=(
                    scheduled_clock != self._baseline_rebalance_clock
                    and commander_output.get("status") == "ACCEPTED"
                ),
                loss_trigger_only=(
                    scheduled_clock != self._baseline_rebalance_clock
                    and commander_output.get("status") != "ACCEPTED"
                ),
            )
            return {
                "status": str(trading_output["status"]),
                "commander": commander_output,
                "trading": trading_output,
                "orders_created": int(trading_output.get("orders_created", 0)),
                "data_available_cutoff": context["data_available_cutoff"],
                "real_order_routing": False,
            }
        if cycle.cycle_kind == "EXECUTION":
            if self._forward_trading is None:
                return {
                    "status": "FORWARD_TRADING_NOT_CONFIGURED",
                    "fills_created": 0,
                    "real_order_routing": False,
                }
            return self._forward_trading.execute(
                cycle,
                run_id=self._settings.paper_run_id,
                now=now,
            )
        if cycle.cycle_kind == "DAILY_REPORT":
            return {
                "status": "REPORT_READY",
                "paper": self._paper.status(self._settings.paper_run_id),
            }
        if cycle.cycle_kind == "RECONCILIATION":
            paper = self._paper.status(self._settings.paper_run_id)
            return {
                "status": "RECONCILED",
                "arm_count": len(paper.get("arms", {})),
                "real_order_routing": False,
            }
        raise PaperRuntimeError(f"Unsupported paper cycle kind: {cycle.cycle_kind}")

    @property
    def _account_file(self) -> Path:
        if self._settings.paper_account_file is None:
            raise PaperRuntimeError("TRADING_PAPER_ACCOUNT_FILE is not configured")
        return self._settings.paper_account_file


def _run_async_from_worker(awaitable: Awaitable[dict[str, Any]]) -> dict[str, Any]:
    # tick() is intentionally synchronous and is executed in a worker thread by
    # run_forever(), so it owns this short event loop without nesting.
    async def consume() -> dict[str, Any]:
        return await awaitable

    return asyncio.run(consume())


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _scheduled_clock_et(value: datetime) -> str:
    return _aware(value).astimezone(NEW_YORK).strftime("%H:%M")


def _required_lease_owner(cycle: PaperCycleRow) -> str:
    if cycle.lease_owner is None:
        raise PaperRuntimeError(f"Cycle {cycle.cycle_id} has no lease owner")
    return cycle.lease_owner
