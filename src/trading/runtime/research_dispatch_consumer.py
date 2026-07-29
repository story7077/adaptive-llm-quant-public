from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, Protocol

from trading.persistence.research_scheduler import (
    ResearchScheduleFenceError,
    ResearchSchedulerPersistenceError,
    ResearchSchedulerRepository,
)
from trading.research.config import ResearchConfigBundle
from trading.research.contracts import CommanderSelectionV1
from trading.research.dispatch_execution import (
    ResearchWorkExecutionRequestV1,
    ResearchWorkExecutionResultV1,
    build_execution_request,
)
from trading.research.file_runtime import (
    ResearchFileRuntimeError,
    atomic_write_json,
    load_json_model,
)
from trading.research.scheduler import (
    ResearchDispatchTarget,
    ResearchWorkExecutionLeaseV1,
)


class ResearchDispatchConsumerError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class ResearchDispatchExecutor(Protocol):
    async def execute(
        self,
        request: ResearchWorkExecutionRequestV1,
    ) -> ResearchWorkExecutionResultV1: ...


class SubprocessResearchDispatchExecutor:
    """Invoke one trusted local launcher without inheriting trading secrets."""

    def __init__(
        self,
        *,
        executable: Path,
        artifact_root: Path,
        timeout_seconds: int,
    ) -> None:
        resolved = executable.resolve(strict=True)
        if not resolved.is_file():
            raise ResearchDispatchConsumerError(
                "EXECUTOR_IS_NOT_A_REGULAR_FILE"
            )
        if resolved.suffix.lower() not in {".py", ".exe"}:
            raise ResearchDispatchConsumerError(
                "EXECUTOR_TYPE_NOT_PERMITTED"
            )
        if timeout_seconds <= 0:
            raise ValueError("execution timeout must be positive")
        self._executable = resolved
        self._artifact_root = artifact_root.resolve()
        self._timeout_seconds = timeout_seconds

    async def execute(
        self,
        request: ResearchWorkExecutionRequestV1,
    ) -> ResearchWorkExecutionResultV1:
        execution_root = self._artifact_root / request.execution_id
        request_path = execution_root / "request.json"
        result_path = execution_root / "result.json"
        atomic_write_json(request_path, request)
        if result_path.exists():
            result = load_json_model(
                result_path,
                ResearchWorkExecutionResultV1,
            )
            result.assert_bound_to(request)
            return result

        command = self._command(
            request_path=request_path,
            result_path=result_path,
        )
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=self._executable.parent,
            env=_sanitized_executor_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            return_code = await asyncio.wait_for(
                process.wait(),
                timeout=self._timeout_seconds,
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise ResearchDispatchConsumerError(
                "EXECUTOR_TIMEOUT"
            ) from exc
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            raise
        if return_code != 0:
            raise ResearchDispatchConsumerError(
                "EXECUTOR_EXIT_NONZERO"
            )
        if not result_path.is_file():
            raise ResearchDispatchConsumerError(
                "EXECUTOR_RESULT_MISSING"
            )
        try:
            result = load_json_model(
                result_path,
                ResearchWorkExecutionResultV1,
            )
            result.assert_bound_to(request)
        except (ResearchFileRuntimeError, ValueError) as exc:
            raise ResearchDispatchConsumerError(
                "EXECUTOR_RESULT_INVALID"
            ) from exc
        return result

    def _command(
        self,
        *,
        request_path: Path,
        result_path: Path,
    ) -> tuple[str, ...]:
        arguments = (
            "--request",
            str(request_path),
            "--result",
            str(result_path),
        )
        if self._executable.suffix.lower() == ".py":
            return (sys.executable, str(self._executable), *arguments)
        return (str(self._executable), *arguments)


class ResearchDispatchConsumerService:
    def __init__(
        self,
        *,
        repository: ResearchSchedulerRepository,
        config: ResearchConfigBundle,
        executor: ResearchDispatchExecutor,
        selection_provider: Callable[[], CommanderSelectionV1 | None],
    ) -> None:
        safety = config.config.safety
        if (
            safety.real_order_routing
            or safety.automatic_promotion_enabled
            or safety.credential_access
        ):
            raise ResearchDispatchConsumerError(
                "RESEARCH_CONSUMER_SAFETY_CONFIG_INVALID"
            )
        self._repository = repository
        self._config = config
        self._executor = executor
        self._selection_provider = selection_provider

    async def consume_once(
        self,
        *,
        consumer_id: str,
    ) -> dict[str, Any]:
        schedule = self._config.config.schedule
        lease = await asyncio.to_thread(
            self._repository.claim_execution,
            consumer_id=consumer_id,
            lease_seconds=schedule.dispatch_lease_seconds,
        )
        if lease is None:
            return {
                "consumed": False,
                "reason_code": "NO_DISPATCHED_RESEARCH_WORK",
                "config_manifest_hash": self._config.manifest_hash,
                "automatic_promotion_enabled": False,
                "real_order_routing": False,
            }
        active_lease = lease
        try:
            plan, receipt = await asyncio.to_thread(
                self._repository.execution_input,
                execution_lease=active_lease,
            )
            commander_selection = None
            if (
                receipt.dispatch_target
                is ResearchDispatchTarget.DEEP_RESEARCH_CYCLE_V1
            ):
                commander_selection = self._selection_provider()
                if commander_selection is None:
                    raise ResearchDispatchConsumerError(
                        "NO_RESEARCH_COMMANDER_SELECTED"
                    )
            request = build_execution_request(
                execution_lease=active_lease,
                plan=plan,
                receipt=receipt,
                commander_selection=commander_selection,
            )
            result, active_lease = await self._execute_with_renewal(
                request=request,
                execution_lease=active_lease,
            )
            result.assert_bound_to(request)
            if commander_selection is not None:
                current_selection = self._selection_provider()
                if current_selection != commander_selection:
                    raise ResearchDispatchConsumerError(
                        "STALE_COMMANDER_SELECTION"
                    )
            created = await asyncio.to_thread(
                self._repository.record_execution_outcome,
                execution_lease=active_lease,
                succeeded=True,
                reason_code=None,
                maximum_attempts=schedule.maximum_dispatch_attempts,
                result=result,
            )
            return {
                "consumed": True,
                "created": created,
                "work_item_id": lease.work_item_id,
                "work_kind": lease.work_kind.value,
                "execution_id": lease.execution_id,
                "result_hash": result.result_hash,
                "research_cycle_id": result.research_cycle_id,
                "artifact_count": len(result.artifacts),
                "invocation_count": len(result.invocations),
                "config_manifest_hash": self._config.manifest_hash,
                "automatic_promotion_enabled": False,
                "real_order_routing": False,
            }
        except ResearchScheduleFenceError:
            return {
                "consumed": False,
                "stale_consumer": True,
                "work_item_id": lease.work_item_id,
                "execution_id": lease.execution_id,
                "config_manifest_hash": self._config.manifest_hash,
                "automatic_promotion_enabled": False,
                "real_order_routing": False,
            }
        except Exception as exc:
            reason_code = _execution_reason_code(exc)
            try:
                recorded = await asyncio.to_thread(
                    self._repository.record_execution_outcome,
                    execution_lease=active_lease,
                    succeeded=False,
                    reason_code=reason_code,
                    maximum_attempts=schedule.maximum_dispatch_attempts,
                    result=None,
                )
            except ResearchScheduleFenceError:
                return {
                    "consumed": False,
                    "stale_consumer": True,
                    "work_item_id": lease.work_item_id,
                    "execution_id": lease.execution_id,
                    "config_manifest_hash": self._config.manifest_hash,
                    "automatic_promotion_enabled": False,
                    "real_order_routing": False,
                }
            return {
                "consumed": False,
                "failed": True,
                "failure_recorded": recorded,
                "work_item_id": lease.work_item_id,
                "execution_id": lease.execution_id,
                "reason_code": reason_code,
                "config_manifest_hash": self._config.manifest_hash,
                "automatic_promotion_enabled": False,
                "real_order_routing": False,
            }

    async def run_forever(
        self,
        *,
        consumer_id: str,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        stop = stop_event or asyncio.Event()
        poll_seconds = self._config.config.schedule.worker_poll_seconds
        while not stop.is_set():
            await self.consume_once(consumer_id=consumer_id)
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    stop.wait(),
                    timeout=poll_seconds,
                )

    async def _execute_with_renewal(
        self,
        *,
        request: ResearchWorkExecutionRequestV1,
        execution_lease: ResearchWorkExecutionLeaseV1,
    ) -> tuple[
        ResearchWorkExecutionResultV1,
        ResearchWorkExecutionLeaseV1,
    ]:
        lease_seconds = self._config.config.schedule.dispatch_lease_seconds
        renewal_seconds = max(1.0, lease_seconds / 3)
        task = asyncio.create_task(self._executor.execute(request))
        active_lease = execution_lease
        try:
            while True:
                done, _ = await asyncio.wait(
                    {task},
                    timeout=renewal_seconds,
                )
                if task in done:
                    return task.result(), active_lease
                active_lease = await asyncio.to_thread(
                    self._repository.renew_execution_lease,
                    execution_lease=active_lease,
                    lease_seconds=lease_seconds,
                )
        except Exception:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            raise


def _sanitized_executor_environment() -> dict[str, str]:
    permitted = (
        "ALLUSERSPROFILE",
        "APPDATA",
        "COMSPEC",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    )
    environment = {
        name: os.environ[name]
        for name in permitted
        if name in os.environ
    }
    environment.update(
        {
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "TRADING_REAL_BROKER_ENABLED": "false",
            "TRADING_PRODUCTION_UNLOCK": "false",
        }
    )
    return environment


def _execution_reason_code(exc: Exception) -> str:
    if isinstance(exc, ResearchDispatchConsumerError):
        return exc.reason_code
    if isinstance(exc, ResearchFileRuntimeError):
        return "RESEARCH_EXECUTION_FILE_CONTRACT_ERROR"
    if isinstance(exc, ResearchSchedulerPersistenceError):
        return "RESEARCH_EXECUTION_PERSISTENCE_ERROR"
    if isinstance(exc, ValueError):
        return "RESEARCH_EXECUTION_CONTRACT_ERROR"
    return f"RESEARCH_EXECUTION_{type(exc).__name__.upper()}"[:80]
