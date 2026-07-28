from __future__ import annotations

import json
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field, JsonValue, TypeAdapter, ValidationError

from trading.domain.contracts import DomainModel
from trading.domain.hashing import canonical_json
from trading.research.candidate_artifact import (
    CandidateArtifactBundleV1,
    CandidateRuntimeV1,
)
from trading.research.candidate_process import (
    CandidateExecutionSecurityV1,
    CandidateProcessResultV1,
    IsolatedCandidateExecutor,
)
from trading.research.config import (
    ResearchConfigBundle,
    candidate_execution_security,
)
from trading.research.contracts import HASH_PATTERN

CandidateExecutionLane = Literal["PRIMARY", "REPLAY"]
_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])


class CommanderCandidateError(RuntimeError):
    """Stable fail-closed error that never relays process output or local paths."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class HostProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class HostProcessRunner(Protocol):
    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: int,
    ) -> HostProcessResult: ...


class SubprocessHostProcessRunner:
    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: int,
    ) -> HostProcessResult:
        try:
            completed = subprocess.run(
                list(args),
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                shell=False,
                timeout=timeout_seconds,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            raise CommanderCandidateError(
                "COMMANDER_CANDIDATE_HOST_TIMEOUT"
            ) from exc
        except OSError as exc:
            raise CommanderCandidateError(
                "COMMANDER_CANDIDATE_HOST_UNAVAILABLE"
            ) from exc
        return HostProcessResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


class CandidateRuntimeAttestationV1(DomainModel):
    schema_version: Literal["candidate_runtime_attestation_v1"]
    isolation_kind: Literal["native_windows_codex_sandbox"]
    isolation_version: Literal["candidate_runtime_v1"]
    candidate_artifact_hash: str = Field(pattern=HASH_PATTERN)
    candidate_tree_hash: str = Field(pattern=HASH_PATTERN)
    runtime: CandidateRuntimeV1
    worker_code_hash: str = Field(pattern=HASH_PATTERN)
    declared_entrypoint: str = Field(
        pattern=r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$"
    )
    network_access_permitted: Literal[False] = False
    credential_access_permitted: Literal[False] = False
    broker_access_permitted: Literal[False] = False
    filesystem_write_permitted: Literal[False] = False
    real_order_routing: Literal[False] = False

    def assert_bound_to(self, bundle: CandidateArtifactBundleV1) -> None:
        if (
            self.candidate_artifact_hash != bundle.bundle_hash
            or self.candidate_tree_hash != bundle.candidate_tree_hash
            or self.runtime != bundle.runtime
            or self.declared_entrypoint != bundle.declared_entrypoint
        ):
            raise CommanderCandidateError(
                "COMMANDER_CANDIDATE_ATTESTATION_MISMATCH"
            )


@dataclass(frozen=True, slots=True)
class CommanderCandidateRuntimeConfig:
    commander_root: Path
    run_root: Path
    python_executable: Path
    host_timeout_seconds: int
    maximum_host_stdout_bytes: int
    maximum_host_stderr_bytes: int

    @classmethod
    def from_paths(
        cls,
        *,
        commander_root: Path,
        run_root: Path,
        research_config: ResearchConfigBundle,
    ) -> CommanderCandidateRuntimeConfig:
        try:
            root = commander_root.resolve(strict=True)
            run = run_root.resolve(strict=True)
        except OSError as exc:
            raise CommanderCandidateError(
                "COMMANDER_CANDIDATE_PATH_UNAVAILABLE"
            ) from exc
        if not root.is_dir() or not run.is_dir() or not run.is_relative_to(root):
            raise CommanderCandidateError("COMMANDER_CANDIDATE_PATH_OUTSIDE_ROOT")
        python_candidates = (
            root / ".venv" / "Scripts" / "python.exe",
            root / ".venv" / "bin" / "python",
        )
        python = next(
            (path.resolve() for path in python_candidates if path.is_file()),
            None,
        )
        if python is None or not python.is_relative_to(root):
            raise CommanderCandidateError(
                "COMMANDER_CANDIDATE_PYTHON_UNAVAILABLE"
            )
        execution = research_config.config.candidate_execution
        return cls(
            commander_root=root,
            run_root=run,
            python_executable=python,
            host_timeout_seconds=execution.host_process_timeout_seconds,
            maximum_host_stdout_bytes=execution.maximum_host_stdout_bytes,
            maximum_host_stderr_bytes=execution.maximum_host_stderr_bytes,
        )


class CommanderCandidateProcessTransport:
    """Invoke one immutable Candidate lane through the separate Commander repo."""

    def __init__(
        self,
        *,
        config: CommanderCandidateRuntimeConfig,
        execution_lane: CandidateExecutionLane,
        runner: HostProcessRunner,
    ) -> None:
        self._config = config
        self._execution_lane = execution_lane
        self._runner = runner

    def invoke(
        self,
        *,
        request_json: str,
        request_hash: str,
        security: CandidateExecutionSecurityV1,
    ) -> CandidateProcessResultV1:
        del request_hash
        with tempfile.TemporaryDirectory(prefix="candidate-host-") as temporary:
            temporary_root = Path(temporary)
            request_path = temporary_root / "request.json"
            security_path = temporary_root / "security.json"
            request_path.write_text(request_json, encoding="utf-8", newline="\n")
            security_path.write_text(
                canonical_json(security),
                encoding="utf-8",
                newline="\n",
            )
            payload = self._run_json(
                (
                    *self._base_command(),
                    "invoke-candidate",
                    "--run",
                    str(self._config.run_root),
                    "--request",
                    str(request_path),
                    "--security",
                    str(security_path),
                    "--execution-lane",
                    self._execution_lane,
                )
            )
        try:
            return CandidateProcessResultV1.model_validate(payload)
        except ValidationError as exc:
            raise CommanderCandidateError(
                "COMMANDER_CANDIDATE_RESULT_INVALID"
            ) from exc

    def runtime_attestation(self) -> CandidateRuntimeAttestationV1:
        payload = self._run_json(
            (
                *self._base_command(),
                "candidate-runtime-info",
                "--run",
                str(self._config.run_root),
            )
        )
        try:
            return CandidateRuntimeAttestationV1.model_validate(payload)
        except ValidationError as exc:
            raise CommanderCandidateError(
                "COMMANDER_CANDIDATE_ATTESTATION_INVALID"
            ) from exc

    def _base_command(self) -> tuple[str, ...]:
        return (
            str(self._config.python_executable),
            "-I",
            "-m",
            "research_commander.cli",
        )

    def _run_json(self, args: Sequence[str]) -> dict[str, JsonValue]:
        result = self._runner.run(
            args,
            cwd=self._config.commander_root,
            timeout_seconds=self._config.host_timeout_seconds,
        )
        if (
            len(result.stdout) > self._config.maximum_host_stdout_bytes
            or len(result.stderr) > self._config.maximum_host_stderr_bytes
        ):
            raise CommanderCandidateError(
                "COMMANDER_CANDIDATE_HOST_OUTPUT_LIMIT"
            )
        if result.returncode != 0:
            raise CommanderCandidateError(
                "COMMANDER_CANDIDATE_HOST_COMMAND_FAILED"
            )
        try:
            decoded = result.stdout.decode("utf-8", errors="strict")
            payload = _JSON_OBJECT_ADAPTER.validate_python(json.loads(decoded))
        except (UnicodeError, json.JSONDecodeError, ValidationError) as exc:
            raise CommanderCandidateError(
                "COMMANDER_CANDIDATE_HOST_RESPONSE_INVALID"
            ) from exc
        return payload


@dataclass(frozen=True, slots=True)
class ConnectedCandidateRuntime:
    attestation: CandidateRuntimeAttestationV1
    security: CandidateExecutionSecurityV1
    primary_executor: IsolatedCandidateExecutor
    replay_executor: IsolatedCandidateExecutor


def connect_candidate_runtime(
    *,
    bundle: CandidateArtifactBundleV1,
    commander_root: Path,
    run_root: Path,
    research_config: ResearchConfigBundle,
    runner: HostProcessRunner | None = None,
) -> ConnectedCandidateRuntime:
    runtime_config = CommanderCandidateRuntimeConfig.from_paths(
        commander_root=commander_root,
        run_root=run_root,
        research_config=research_config,
    )
    process_runner = runner or SubprocessHostProcessRunner()
    primary_transport = CommanderCandidateProcessTransport(
        config=runtime_config,
        execution_lane="PRIMARY",
        runner=process_runner,
    )
    attestation = primary_transport.runtime_attestation()
    attestation.assert_bound_to(bundle)
    security = candidate_execution_security(
        research_config,
        candidate_artifact_hash=bundle.bundle_hash,
        candidate_tree_hash=bundle.candidate_tree_hash,
        runtime_executable_hash=attestation.runtime.executable_sha256,
        worker_code_hash=attestation.worker_code_hash,
        declared_entrypoint=bundle.declared_entrypoint,
    )
    replay_transport = CommanderCandidateProcessTransport(
        config=runtime_config,
        execution_lane="REPLAY",
        runner=process_runner,
    )
    return ConnectedCandidateRuntime(
        attestation=attestation,
        security=security,
        primary_executor=IsolatedCandidateExecutor(
            security=security,
            transport=primary_transport,
        ),
        replay_executor=IsolatedCandidateExecutor(
            security=security,
            transport=replay_transport,
        ),
    )
