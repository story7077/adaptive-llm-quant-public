from __future__ import annotations

import json
from typing import Literal, Protocol, Self

from pydantic import Field, model_validator

from trading.domain.contracts import DomainModel
from trading.domain.hashing import canonical_hash, canonical_json
from trading.research.candidate_abi import (
    CandidateDecisionRequestV1,
    CandidateDecisionResponseV1,
)
from trading.research.contracts import HASH_PATTERN, IDENTIFIER_PATTERN


class CandidateProcessLimitsV1(DomainModel):
    timeout_seconds: int = Field(gt=0)
    maximum_stdout_bytes: int = Field(gt=0)
    maximum_stderr_bytes: int = Field(gt=0)
    maximum_memory_bytes: int = Field(gt=0)
    maximum_processes: int = Field(gt=0)


class CandidateExecutionSecurityV1(DomainModel):
    """Versioned attestation required from the isolated execution service."""

    schema_version: str = Field(default="candidate_execution_security_v1")
    isolation_kind: str = Field(pattern=IDENTIFIER_PATTERN)
    isolation_version: str = Field(pattern=IDENTIFIER_PATTERN)
    candidate_artifact_hash: str = Field(pattern=HASH_PATTERN)
    candidate_tree_hash: str = Field(pattern=HASH_PATTERN)
    runtime_executable_hash: str = Field(pattern=HASH_PATTERN)
    worker_code_hash: str = Field(pattern=HASH_PATTERN)
    declared_entrypoint: str = Field(
        pattern=r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$"
    )
    limits: CandidateProcessLimitsV1
    network_access_permitted: Literal[False] = False
    credential_access_permitted: Literal[False] = False
    broker_access_permitted: Literal[False] = False
    filesystem_write_permitted: Literal[False] = False
    real_order_routing: Literal[False] = False
    security_contract_hash: str = Field(pattern=HASH_PATTERN)

    @model_validator(mode="after")
    def validate_hash(self) -> Self:
        payload = self.model_dump(mode="python", exclude={"security_contract_hash"})
        if canonical_hash(payload) != self.security_contract_hash:
            raise ValueError("candidate execution security hash mismatch")
        return self


class CandidateProcessResultV1(DomainModel):
    schema_version: str = Field(default="candidate_process_result_v1")
    invocation_id: str = Field(pattern=IDENTIFIER_PATTERN)
    request_hash: str = Field(pattern=HASH_PATTERN)
    candidate_artifact_hash: str = Field(pattern=HASH_PATTERN)
    security_contract_hash: str = Field(pattern=HASH_PATTERN)
    exit_code: int | None
    timed_out: bool
    resource_limit_exceeded: bool
    stdout_utf8: str
    stdout_sha256: str = Field(pattern=HASH_PATTERN)
    stderr_sha256: str = Field(pattern=HASH_PATTERN)
    stdout_bytes: int = Field(ge=0)
    stderr_bytes: int = Field(ge=0)
    network_access_permitted: Literal[False] = False
    credential_access_permitted: Literal[False] = False
    broker_access_permitted: Literal[False] = False
    filesystem_write_permitted: Literal[False] = False
    real_order_routing: Literal[False] = False
    result_hash: str = Field(pattern=HASH_PATTERN)

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        encoded = self.stdout_utf8.encode("utf-8")
        if len(encoded) != self.stdout_bytes:
            raise ValueError("candidate stdout byte count mismatch")
        import hashlib

        if hashlib.sha256(encoded).hexdigest() != self.stdout_sha256:
            raise ValueError("candidate stdout hash mismatch")
        payload = self.model_dump(mode="python", exclude={"result_hash"})
        if canonical_hash(payload) != self.result_hash:
            raise ValueError("candidate process result hash mismatch")
        return self


class CandidateProcessTransport(Protocol):
    def invoke(
        self,
        *,
        request_json: str,
        request_hash: str,
        security: CandidateExecutionSecurityV1,
    ) -> CandidateProcessResultV1: ...


class IsolatedCandidateExecutor:
    """Parse only a successful, attested, single-object candidate response."""

    def __init__(
        self,
        *,
        security: CandidateExecutionSecurityV1,
        transport: CandidateProcessTransport,
    ) -> None:
        self._security = CandidateExecutionSecurityV1.model_validate(
            security.model_dump(mode="python")
        )
        self._transport = transport

    def execute(
        self,
        request: CandidateDecisionRequestV1,
    ) -> CandidateDecisionResponseV1:
        if request.candidate_artifact_hash != self._security.candidate_artifact_hash:
            raise ValueError("candidate request differs from execution artifact")
        result = CandidateProcessResultV1.model_validate(
            self._transport.invoke(
                request_json=canonical_json(request),
                request_hash=request.request_hash,
                security=self._security,
            )
        )
        self._assert_process_result(result, request)
        try:
            decoded = json.loads(result.stdout_utf8)
        except (UnicodeError, json.JSONDecodeError):
            raise ValueError("candidate response is not one valid JSON value") from None
        if not isinstance(decoded, dict):
            raise ValueError("candidate response must be a JSON object")
        response = CandidateDecisionResponseV1.model_validate(decoded)
        response.assert_bound_to(request)
        return response

    def _assert_process_result(
        self,
        result: CandidateProcessResultV1,
        request: CandidateDecisionRequestV1,
    ) -> None:
        limits = self._security.limits
        if (
            result.request_hash != request.request_hash
            or result.candidate_artifact_hash != request.candidate_artifact_hash
            or result.security_contract_hash
            != self._security.security_contract_hash
        ):
            raise ValueError("candidate process result binding mismatch")
        if result.stdout_bytes > limits.maximum_stdout_bytes:
            raise ValueError("candidate stdout exceeded configured limit")
        if result.stderr_bytes > limits.maximum_stderr_bytes:
            raise ValueError("candidate stderr exceeded configured limit")
        if result.timed_out:
            raise ValueError("candidate execution timed out")
        if result.resource_limit_exceeded:
            raise ValueError("candidate execution exceeded a resource limit")
        if result.exit_code != 0:
            raise ValueError("candidate process exited unsuccessfully")


def build_candidate_execution_security(
    *,
    isolation_kind: str,
    isolation_version: str,
    candidate_artifact_hash: str,
    candidate_tree_hash: str,
    runtime_executable_hash: str,
    worker_code_hash: str,
    declared_entrypoint: str,
    limits: CandidateProcessLimitsV1,
) -> CandidateExecutionSecurityV1:
    payload = {
        "schema_version": "candidate_execution_security_v1",
        "isolation_kind": isolation_kind,
        "isolation_version": isolation_version,
        "candidate_artifact_hash": candidate_artifact_hash,
        "candidate_tree_hash": candidate_tree_hash,
        "runtime_executable_hash": runtime_executable_hash,
        "worker_code_hash": worker_code_hash,
        "declared_entrypoint": declared_entrypoint,
        "limits": limits,
        "network_access_permitted": False,
        "credential_access_permitted": False,
        "broker_access_permitted": False,
        "filesystem_write_permitted": False,
        "real_order_routing": False,
    }
    return CandidateExecutionSecurityV1.model_validate(
        {**payload, "security_contract_hash": canonical_hash(payload)}
    )


def build_candidate_process_result(
    *,
    invocation_id: str,
    request_hash: str,
    candidate_artifact_hash: str,
    security_contract_hash: str,
    exit_code: int | None,
    timed_out: bool,
    resource_limit_exceeded: bool,
    stdout_utf8: str,
    stderr_sha256: str,
    stderr_bytes: int,
) -> CandidateProcessResultV1:
    import hashlib

    encoded = stdout_utf8.encode("utf-8")
    payload = {
        "schema_version": "candidate_process_result_v1",
        "invocation_id": invocation_id,
        "request_hash": request_hash,
        "candidate_artifact_hash": candidate_artifact_hash,
        "security_contract_hash": security_contract_hash,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "resource_limit_exceeded": resource_limit_exceeded,
        "stdout_utf8": stdout_utf8,
        "stdout_sha256": hashlib.sha256(encoded).hexdigest(),
        "stderr_sha256": stderr_sha256,
        "stdout_bytes": len(encoded),
        "stderr_bytes": stderr_bytes,
        "network_access_permitted": False,
        "credential_access_permitted": False,
        "broker_access_permitted": False,
        "filesystem_write_permitted": False,
        "real_order_routing": False,
    }
    return CandidateProcessResultV1.model_validate(
        {**payload, "result_hash": canonical_hash(payload)}
    )
