from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime
from typing import Any

from trading.domain.time import Clock, SystemClock, require_aware_utc
from trading.persistence.research_scheduler import (
    ResearchScheduleFenceError,
    ResearchSchedulerRepository,
)
from trading.research.config import ResearchConfigBundle
from trading.research.scheduler import (
    ResearchSchedulePlanV1,
    ResearchWorkDispatchReceiptV1,
    build_due_schedule_plans,
    build_operator_deep_research_plan,
)


class ResearchSchedulerConfigurationError(RuntimeError):
    pass


class ResearchSchedulerPlanningError(RuntimeError):
    pass


class ResearchSchedulerService:
    """Plans and dispatches research work without invoking a model or broker."""

    def __init__(
        self,
        *,
        repository: ResearchSchedulerRepository,
        config: ResearchConfigBundle,
        clock: Clock | None = None,
    ) -> None:
        if config.config.safety.real_order_routing:
            raise ResearchSchedulerConfigurationError(
                "Research scheduler requires real_order_routing=false"
            )
        self._repository = repository
        self._config = config
        self._clock = clock or SystemClock()

    def plan(
        self,
        *,
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        instant = require_aware_utc(as_of or self._clock.now())
        schedule = self._config.config.schedule
        sessions, evidence, consumed = self._repository.planning_inputs(
            as_of=instant,
            calendar_version=schedule.market_calendar_version,
        )
        plans = build_due_schedule_plans(
            schedule=schedule,
            config_manifest_hash=self._config.manifest_hash,
            as_of=instant,
            market_sessions=sessions,
            evidence=evidence,
            consumed_evidence_hashes=consumed,
            include_outcome_maintenance=(
                self._config.config.recursive_improvement.enabled
            ),
        )
        created = self._repository.store_plans(
            plans,
            created_at=instant,
        )
        return {
            "as_of": instant.isoformat().replace("+00:00", "Z"),
            "planned_count": len(plans),
            "created_count": created,
            "work_item_ids": [item.work_item_id for item in plans],
            "plan_hashes": [item.plan_hash for item in plans],
            "config_manifest_hash": self._config.manifest_hash,
            "schedule_version": schedule.schedule_version,
            "real_order_routing": False,
        }

    def plan_operator_deep_research(
        self,
        *,
        operator_trigger_id: str,
        operator_reason_code: str,
        calendar_session_id: str,
        scheduled_for: datetime,
        data_available_cutoff: datetime,
        created_at: datetime | None = None,
    ) -> dict[str, Any]:
        instant = require_aware_utc(created_at or self._clock.now())
        schedule = self._config.config.schedule
        sessions, _, _ = self._repository.planning_inputs(
            as_of=instant,
            calendar_version=schedule.market_calendar_version,
        )
        matching = tuple(
            session
            for session in sessions
            if session.calendar_session_id == calendar_session_id
        )
        if len(matching) != 1:
            raise ResearchSchedulerPlanningError(
                "operator deep research requires one available versioned "
                f"calendar session: {calendar_session_id}"
            )
        plan = build_operator_deep_research_plan(
            schedule=schedule,
            config_manifest_hash=self._config.manifest_hash,
            operator_trigger_id=operator_trigger_id,
            operator_reason_code=operator_reason_code,
            scheduled_for=scheduled_for,
            data_available_cutoff=data_available_cutoff,
            session=matching[0],
        )
        created = self._repository.store_plans(
            (plan,),
            created_at=instant,
        )
        return {
            "created_at": instant.isoformat().replace("+00:00", "Z"),
            "created_count": created,
            "plan": plan.model_dump(mode="json", exclude_none=True),
            "config_manifest_hash": self._config.manifest_hash,
            "schedule_version": schedule.schedule_version,
            "automatic_promotion_enabled": False,
            "real_order_routing": False,
        }

    def dispatch_once(
        self,
        *,
        worker_id: str,
    ) -> dict[str, Any]:
        schedule = self._config.config.schedule
        lease = self._repository.claim_next(
            lease_owner=worker_id,
            lease_seconds=schedule.dispatch_lease_seconds,
            maximum_attempts=schedule.maximum_dispatch_attempts,
        )
        if lease is None:
            return {
                "dispatched": False,
                "reason_code": "NO_DUE_RESEARCH_WORK",
                "config_manifest_hash": self._config.manifest_hash,
                "real_order_routing": False,
            }
        try:
            receipt = self._repository.commit_dispatch(lease=lease)
        except ResearchScheduleFenceError:
            return {
                "dispatched": False,
                "stale_worker": True,
                "work_item_id": lease.work_item_id,
                "attempt_number": lease.attempt_number,
                "config_manifest_hash": self._config.manifest_hash,
                "real_order_routing": False,
            }
        except Exception as exc:
            reason_code = type(exc).__name__.upper()
            try:
                self._repository.fail_dispatch(
                    lease=lease,
                    reason_code=reason_code,
                    maximum_attempts=schedule.maximum_dispatch_attempts,
                )
            except ResearchScheduleFenceError:
                return {
                    "dispatched": False,
                    "stale_worker": True,
                    "work_item_id": lease.work_item_id,
                    "attempt_number": lease.attempt_number,
                    "config_manifest_hash": self._config.manifest_hash,
                    "real_order_routing": False,
                }
            return {
                "dispatched": False,
                "failed": True,
                "work_item_id": lease.work_item_id,
                "attempt_number": lease.attempt_number,
                "reason_code": reason_code,
                "config_manifest_hash": self._config.manifest_hash,
                "real_order_routing": False,
            }
        return _receipt_result(receipt)

    def tick(
        self,
        *,
        worker_id: str,
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        planning = self.plan(as_of=as_of)
        dispatch = self.dispatch_once(worker_id=worker_id)
        return {
            "planning": planning,
            "dispatch": dispatch,
            "real_order_routing": False,
        }

    async def run_forever(
        self,
        *,
        worker_id: str,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        stop = stop_event or asyncio.Event()
        poll_seconds = self._config.config.schedule.worker_poll_seconds
        while not stop.is_set():
            await asyncio.to_thread(
                self.tick,
                worker_id=worker_id,
            )
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    stop.wait(),
                    timeout=poll_seconds,
                )

    def status(self) -> dict[str, Any]:
        schedule = self._config.config.schedule
        return self._repository.status(
            history_limit=schedule.status_history_limit,
            schedule_version=schedule.schedule_version,
            config_manifest_hash=self._config.manifest_hash,
            timezone=schedule.timezone,
        )


def _receipt_result(
    receipt: ResearchWorkDispatchReceiptV1,
) -> dict[str, Any]:
    return {
        "dispatched": True,
        "receipt": receipt.model_dump(mode="json"),
        "real_order_routing": False,
    }


def planned_work_from_status(
    plan: ResearchSchedulePlanV1,
) -> dict[str, Any]:
    return {
        "work_item_id": plan.work_item_id,
        "work_kind": plan.work_kind.value,
        "scheduled_for": plan.scheduled_for.isoformat().replace(
            "+00:00",
            "Z",
        ),
        "plan_hash": plan.plan_hash,
        "real_order_routing": False,
    }
