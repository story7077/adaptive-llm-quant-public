from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from trading.control.bundles import export_request_bundle, run_codex_bundle
from trading.control.providers import CommanderProvider
from trading.control.service import ControlPlaneService
from trading.persistence.models import PaperCycleRow
from trading.runtime.news import PaperNewsPipeline
from trading.runtime.paper import PaperRuntimeService
from trading.settings import Settings


class OperationalRiskCommander:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        settings: Settings,
        paper: PaperRuntimeService,
        news: PaperNewsPipeline,
        repo_root: Path,
    ) -> None:
        self._settings = settings
        self._paper = paper
        self._news = news
        self._service = ControlPlaneService(session_factory)
        self._commander_dir = settings.commander_dir or repo_root.parent / "stock-commander"

    async def run(
        self,
        cycle: PaperCycleRow,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._run_sync, cycle, context)

    def _run_sync(
        self,
        cycle: PaperCycleRow,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        cutoff = context.get("data_available_cutoff")
        if not isinstance(cutoff, datetime):
            raise ValueError("DECISION cycle context requires a datetime cutoff")
        request = self._service.request_for_cycle(
            scope_id=self._settings.paper_run_id,
            arm_scope="B3-RISK",
            cycle_id=cycle.cycle_id,
        )
        if request is None:
            news_context = self._news.recent_context(
                as_of=cutoff,
                run_id=self._settings.paper_run_id,
            )
            if not news_context:
                return {
                    "status": "NO_CHANGE",
                    "reason_code": "NO_VALIDATED_NEWS_CONTEXT",
                    "orders_created": 0,
                }
            if not self._settings.real_llm_enabled:
                return {
                    "status": "NO_CHANGE",
                    "reason_code": "CODEX_MODEL_GATE_DISABLED",
                    "orders_created": 0,
                }

            selection = self._service.current_selection()
            if selection is None:
                return {
                    "status": "NO_CHANGE",
                    "reason_code": "NO_COMMANDER_SELECTED",
                    "orders_created": 0,
                }
            if selection.provider is not CommanderProvider.CODEX_SOL_MAX:
                return {
                    "status": "NO_CHANGE",
                    "reason_code": "SELECTED_COMMANDER_NOT_AUTOMATED",
                    "selected_provider": selection.provider.value,
                    "orders_created": 0,
                }

            request = self._service.create_request(
                arm_scope="B3-RISK",
                scope_id=self._settings.paper_run_id,
                context={
                    "paper_state": self._paper.bounded_decision_context(
                        self._settings.paper_run_id,
                        as_of=cutoff,
                    ),
                    "news_analyses": news_context,
                    "strategy_readiness": {
                        "T1": "BLOCKED_UNTIL_PIT_SOXX_MEMBERSHIP_AND_OOS_CALIBRATION",
                        "R1": (
                            "WARMING_60_COMPLETE_INTRADAY_SESSIONS_"
                            "20_SAME_CLOCK_SESSIONS_AND_OOS_CALIBRATION"
                        ),
                        "X1": "BLOCKED_UNTIL_VERSIONED_OOS_CALIBRATION",
                    },
                    "cycle": {
                        "cycle_id": cycle.cycle_id,
                        "scheduled_at": cycle.scheduled_at.isoformat(),
                        "data_available_cutoff": cutoff.isoformat(),
                    },
                    "hard_constraints": {
                        "real_order_routing": False,
                        "pre_existing_concentration_breach": True,
                        "new_breach_increase_forbidden": True,
                        "soxl_mode": "SELL_ONLY",
                    },
                },
                as_of=cutoff,
                data_available_cutoff=cutoff,
            )
        if request.provider is not CommanderProvider.CODEX_SOL_MAX:
            return {
                "status": "NO_CHANGE",
                "reason_code": "CYCLE_REQUEST_NOT_AUTOMATED",
                "selected_provider": request.provider.value,
                "orders_created": 0,
            }
        existing_receipt = self._service.receipt_for_request(request.request_id)
        if existing_receipt is not None:
            return {
                "status": existing_receipt.status,
                "reason_code": existing_receipt.reason_code,
                "request_id": request.request_id,
                "decision_id": existing_receipt.decision_id,
                "applied_policy_version": existing_receipt.applied_policy_version,
                "orders_created": 0,
                "data_available_cutoff": cutoff,
                "completed_at": existing_receipt.created_at,
                "idempotent_replay": True,
            }
        bundle = export_request_bundle(request, commander_dir=self._commander_dir)
        output = run_codex_bundle(bundle, timeout_seconds=1200)
        if cycle.lease_owner is None:
            raise ValueError("DECISION cycle has no lease owner")
        receipt = self._service.submit_decision(
            request_id=request.request_id,
            provider=CommanderProvider.CODEX_SOL_MAX,
            output=output,
            cycle_id=cycle.cycle_id,
            cycle_lease_owner=cycle.lease_owner,
            cycle_attempt_count=cycle.attempt_count,
        )
        return {
            "status": receipt.status,
            "reason_code": receipt.reason_code,
            "request_id": request.request_id,
            "decision_id": receipt.decision_id,
            "applied_policy_version": receipt.applied_policy_version,
            "orders_created": 0,
            "data_available_cutoff": cutoff,
            "completed_at": receipt.created_at,
            "idempotent_replay": receipt.idempotent_replay,
        }
