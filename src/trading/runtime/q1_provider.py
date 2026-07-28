from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from threading import Lock
from typing import Any, cast

from sqlalchemy.orm import Session, sessionmaker

from trading.control.contracts import SelectionSnapshot
from trading.control.providers import CommanderProvider
from trading.control.service import ControlPlaneService
from trading.domain.hashing import canonical_data, canonical_hash, canonical_json
from trading.llm.q1_overlay import (
    Q1LlmOverlayDecision,
    validate_bounded_evidence,
)
from trading.runtime.q1_config import Q1LlmTransportConfig
from trading.settings import Settings

Q1_LLM_REVIEW_REQUEST_SCHEMA_VERSION = "q1_llm_review_request_v1"
Q1_BUNDLE_SCHEMA_VERSION = "q1_commander_bundle_v1"
DEFAULT_AUDIT_REQUEST_CAPACITY = 256
DEFAULT_AUDIT_ATTEMPTS_PER_REQUEST = 8
_HASH_LENGTH = 64
_REQUEST_ID_MAX_LENGTH = 100
_EXPECTED_REQUEST_KEYS = frozenset(
    {
        "schema_version",
        "request_id",
        "context_manifest_hash",
        "calendar_session_id",
        "scheduled_at",
        "portfolio_state_as_of",
        "quote_as_of",
        "q1_det",
        "q1_llm",
        "quotes",
        "news_events",
        "allowed_evidence_event_ids",
        "allowed_outputs",
        "real_order_routing",
    }
)
_SENSITIVE_KEY_TOKENS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "credential",
        "credentials",
        "password",
        "private_key",
        "secret",
        "secret_key",
        "session_token",
        "tax_id",
    }
)
_SENSITIVE_KEY_FRAGMENTS = (
    "access_token",
    "api_key",
    "authorization",
    "cookie",
    "credential",
    "password",
    "private_key",
    "secret",
    "session_token",
    "tax_id",
)


@dataclass(frozen=True, slots=True)
class Q1CommanderBundle:
    bundle_hash: str
    directory: Path
    request_file: Path
    schema_file: Path
    prompt_file: Path
    output_file: Path
    provider: CommanderProvider
    selection_version: int
    transport_config: Q1LlmTransportConfig
    codex_command: tuple[str, ...] | None


class Q1ProviderAuditStatus(StrEnum):
    GATE_DISABLED = "GATE_DISABLED"
    INVALID_REQUEST = "INVALID_REQUEST"
    SELECTION_LOOKUP_FAILED = "SELECTION_LOOKUP_FAILED"
    NO_SELECTION = "NO_SELECTION"
    BUNDLE_EXPORT_FAILED = "BUNDLE_EXPORT_FAILED"
    TRANSPORT_TIMEOUT = "TRANSPORT_TIMEOUT"
    TRANSPORT_FAILED = "TRANSPORT_FAILED"
    TRANSPORT_NO_OUTPUT = "TRANSPORT_NO_OUTPUT"
    OUTPUT_INVALID = "OUTPUT_INVALID"
    SELECTION_RECHECK_FAILED = "SELECTION_RECHECK_FAILED"
    STALE_SELECTION = "STALE_SELECTION"
    VALIDATED = "VALIDATED"


@dataclass(frozen=True, slots=True)
class Q1ProviderAuditRecord:
    request_id: str
    attempt_index: int
    selection_id: str | None
    selection_version: int | None
    provider: str | None
    model: str | None
    reasoning_profile: str | None
    config_hash: str | None
    bundle_hash: str | None
    transport_config_hash: str
    validated_output_hash: str | None
    status: Q1ProviderAuditStatus

    def as_payload(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "attempt_index": self.attempt_index,
            "selection_id": self.selection_id,
            "selection_version": self.selection_version,
            "provider": self.provider,
            "model": self.model,
            "reasoning_profile": self.reasoning_profile,
            "config_hash": self.config_hash,
            "bundle_hash": self.bundle_hash,
            "transport_config_hash": self.transport_config_hash,
            "validated_output_hash": self.validated_output_hash,
            "status": self.status.value,
        }


Q1CodexRunner = Callable[
    [Q1CommanderBundle],
    Mapping[str, object] | Q1LlmOverlayDecision | None,
]


class Q1SelectedCommanderProvider:
    """Dispatch a bounded Q1 review to the currently selected commander.

    The adapter is deliberately fail-closed. It owns no trading or policy state:
    the caller remains responsible for applying the validated reduce-only overlay.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        settings: Settings,
        transport_config: Q1LlmTransportConfig,
        repo_root: Path | None = None,
        codex_runner: Q1CodexRunner | None = None,
        audit_request_capacity: int = DEFAULT_AUDIT_REQUEST_CAPACITY,
        audit_attempts_per_request: int = DEFAULT_AUDIT_ATTEMPTS_PER_REQUEST,
    ) -> None:
        if audit_request_capacity <= 0:
            raise ValueError("Q1 provider audit request capacity must be positive")
        if audit_attempts_per_request <= 0:
            raise ValueError("Q1 provider audit attempt capacity must be positive")
        self._settings = settings
        self._transport_config = transport_config
        self._transport_config_hash = canonical_hash(
            {
                "provider_timeout_seconds": (
                    transport_config.provider_timeout_seconds
                ),
                "transport_timeout_seconds": (
                    transport_config.transport_timeout_seconds
                ),
                "transport_poll_interval_seconds": (
                    transport_config.transport_poll_interval_seconds
                ),
            }
        )
        self._service = ControlPlaneService(session_factory)
        self._audit_request_capacity = audit_request_capacity
        self._audit_attempts_per_request = audit_attempts_per_request
        self._audit_lock = Lock()
        self._audits: OrderedDict[
            str,
            tuple[Q1ProviderAuditRecord, ...],
        ] = OrderedDict()
        root = repo_root or Path.cwd()
        self._commander_dir = (
            settings.commander_dir or (root.parent / "stock-commander")
        ).resolve()
        if codex_runner is None:
            def default_runner(
                bundle: Q1CommanderBundle,
            ) -> Mapping[str, object] | None:
                return run_q1_codex_bundle(bundle)

            self._codex_runner: Q1CodexRunner = default_runner
        else:
            self._codex_runner = codex_runner

    def __call__(
        self,
        request: dict[str, Any],
    ) -> Q1LlmOverlayDecision | None:
        request_id = _audit_request_id(request)
        selection: SelectionSnapshot | None = None
        bundle: Q1CommanderBundle | None = None
        if not self._settings.real_llm_enabled:
            return self._finish_attempt(
                request_id=request_id,
                selection=selection,
                bundle=bundle,
                status=Q1ProviderAuditStatus.GATE_DISABLED,
            )
        try:
            normalized_request = _validate_and_normalize_request(request)
            request_id = cast(str, normalized_request["request_id"])
        except Exception:
            return self._finish_attempt(
                request_id=request_id,
                selection=selection,
                bundle=bundle,
                status=Q1ProviderAuditStatus.INVALID_REQUEST,
            )
        try:
            selection = self._service.current_selection()
        except Exception:
            return self._finish_attempt(
                request_id=request_id,
                selection=selection,
                bundle=bundle,
                status=Q1ProviderAuditStatus.SELECTION_LOOKUP_FAILED,
            )
        if selection is None:
            return self._finish_attempt(
                request_id=request_id,
                selection=selection,
                bundle=bundle,
                status=Q1ProviderAuditStatus.NO_SELECTION,
            )
        try:
            bundle = export_q1_commander_bundle(
                normalized_request,
                selection=selection,
                commander_dir=self._commander_dir,
                transport_config=self._transport_config,
            )
        except Exception:
            return self._finish_attempt(
                request_id=request_id,
                selection=selection,
                bundle=bundle,
                status=Q1ProviderAuditStatus.BUNDLE_EXPORT_FAILED,
            )
        try:
            raw_output = _load_output(bundle.output_file)
            if (
                raw_output is None
                and selection.provider is CommanderProvider.CODEX_SOL_MAX
            ):
                raw_output = self._codex_runner(bundle)
            elif (
                raw_output is None
                and selection.provider is CommanderProvider.WEBGPT_SOL_PRO
            ):
                raw_output = _wait_for_output(bundle)
        except (subprocess.TimeoutExpired, TimeoutError):
            return self._finish_attempt(
                request_id=request_id,
                selection=selection,
                bundle=bundle,
                status=Q1ProviderAuditStatus.TRANSPORT_TIMEOUT,
            )
        except Exception:
            return self._finish_attempt(
                request_id=request_id,
                selection=selection,
                bundle=bundle,
                status=Q1ProviderAuditStatus.TRANSPORT_FAILED,
            )
        if raw_output is None:
            status = (
                Q1ProviderAuditStatus.TRANSPORT_TIMEOUT
                if selection.provider is CommanderProvider.WEBGPT_SOL_PRO
                else Q1ProviderAuditStatus.TRANSPORT_NO_OUTPUT
            )
            return self._finish_attempt(
                request_id=request_id,
                selection=selection,
                bundle=bundle,
                status=status,
            )
        try:
            decision = _validate_output(
                raw_output,
                request=normalized_request,
            )
        except Exception:
            return self._finish_attempt(
                request_id=request_id,
                selection=selection,
                bundle=bundle,
                status=Q1ProviderAuditStatus.OUTPUT_INVALID,
            )
        try:
            current_selection = self._service.current_selection()
        except Exception:
            return self._finish_attempt(
                request_id=request_id,
                selection=selection,
                bundle=bundle,
                status=Q1ProviderAuditStatus.SELECTION_RECHECK_FAILED,
                validated_output_hash=_validated_output_hash(decision),
            )
        if not _same_selection(selection, current_selection):
            return self._finish_attempt(
                request_id=request_id,
                selection=selection,
                bundle=bundle,
                status=Q1ProviderAuditStatus.STALE_SELECTION,
                validated_output_hash=_validated_output_hash(decision),
            )
        return self._finish_attempt(
            request_id=request_id,
            selection=selection,
            bundle=bundle,
            status=Q1ProviderAuditStatus.VALIDATED,
            decision=decision,
        )

    def audit_for_request(
        self,
        request_id: str,
    ) -> tuple[Q1ProviderAuditRecord, ...]:
        with self._audit_lock:
            return self._audits.get(request_id, ())

    def _finish_attempt(
        self,
        *,
        request_id: str,
        selection: SelectionSnapshot | None,
        bundle: Q1CommanderBundle | None,
        status: Q1ProviderAuditStatus,
        decision: Q1LlmOverlayDecision | None = None,
        validated_output_hash: str | None = None,
    ) -> Q1LlmOverlayDecision | None:
        output_hash = (
            _validated_output_hash(decision)
            if decision is not None
            else validated_output_hash
        )
        with self._audit_lock:
            existing = self._audits.pop(request_id, ())
            attempt_index = (
                existing[-1].attempt_index + 1
                if existing
                else 1
            )
            record = Q1ProviderAuditRecord(
                request_id=request_id,
                attempt_index=attempt_index,
                selection_id=(
                    None if selection is None else selection.selection_id
                ),
                selection_version=(
                    None if selection is None else selection.version
                ),
                provider=(
                    None if selection is None else selection.provider.value
                ),
                model=None if selection is None else selection.model,
                reasoning_profile=(
                    None
                    if selection is None
                    else selection.reasoning_profile
                ),
                config_hash=(
                    None if selection is None else selection.config_hash
                ),
                bundle_hash=None if bundle is None else bundle.bundle_hash,
                transport_config_hash=self._transport_config_hash,
                validated_output_hash=output_hash,
                status=status,
            )
            retained = (
                *existing[-(self._audit_attempts_per_request - 1):],
                record,
            )
            if self._audit_attempts_per_request == 1:
                retained = (record,)
            self._audits[request_id] = retained
            while len(self._audits) > self._audit_request_capacity:
                self._audits.popitem(last=False)
        return decision


def export_q1_commander_bundle(
    request: Mapping[str, object],
    *,
    selection: SelectionSnapshot,
    commander_dir: Path,
    transport_config: Q1LlmTransportConfig,
) -> Q1CommanderBundle:
    normalized_request = _validate_and_normalize_request(request)
    output_schema = Q1LlmOverlayDecision.model_json_schema()
    bundle_manifest = {
        "schema_version": Q1_BUNDLE_SCHEMA_VERSION,
        "selection": selection.model_dump(mode="json"),
        "transport": {
            "provider_timeout_seconds": (
                transport_config.provider_timeout_seconds
            ),
            "transport_timeout_seconds": (
                transport_config.transport_timeout_seconds
            ),
            "transport_poll_interval_seconds": (
                transport_config.transport_poll_interval_seconds
            ),
        },
        "request": normalized_request,
        "output_schema": output_schema,
    }
    bundle_hash = canonical_hash(bundle_manifest)
    target = commander_dir.resolve() / "q1" / "inbox" / bundle_hash
    request_file = target / "request.json"
    schema_file = target / "output.schema.json"
    prompt_file = target / "prompt.md"
    output_file = target / "output.json"
    _atomic_write_utf8(
        request_file,
        json.dumps(
            normalized_request,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
    )
    _atomic_write_utf8(
        schema_file,
        json.dumps(
            output_schema,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
    )
    _atomic_write_utf8(
        prompt_file,
        _build_prompt(
            selection=selection,
            bundle_hash=bundle_hash,
        ),
    )
    command: tuple[str, ...] | None = None
    if selection.provider is CommanderProvider.CODEX_SOL_MAX:
        command = (
            _codex_executable(),
            "exec",
            "--cd",
            str(commander_dir.resolve()),
            "--skip-git-repo-check",
            "--ephemeral",
            "--ignore-user-config",
            "--strict-config",
            "--model",
            selection.model,
            "-c",
            f'model_reasoning_effort="{selection.reasoning_profile}"',
            "-c",
            'approval_policy="never"',
            "--sandbox",
            "read-only",
            "--output-schema",
            str(schema_file),
            "--output-last-message",
            str(output_file),
            "--json",
            "-",
        )
    return Q1CommanderBundle(
        bundle_hash=bundle_hash,
        directory=target,
        request_file=request_file,
        schema_file=schema_file,
        prompt_file=prompt_file,
        output_file=output_file,
        provider=selection.provider,
        selection_version=selection.version,
        transport_config=transport_config,
        codex_command=command,
    )


def run_q1_codex_bundle(
    bundle: Q1CommanderBundle,
) -> Mapping[str, object] | None:
    if bundle.codex_command is None:
        return None
    prompt = (
        bundle.prompt_file.read_text(encoding="utf-8")
        + "\n\nPrepared request JSON:\n"
        + canonical_json(
            json.loads(bundle.request_file.read_text(encoding="utf-8"))
        )
    )
    completed = subprocess.run(
        bundle.codex_command,
        cwd=bundle.directory,
        input=prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=float(bundle.transport_config.transport_timeout_seconds),
        check=False,
    )
    if completed.returncode != 0:
        return None
    return _load_output(bundle.output_file)


def _wait_for_output(
    bundle: Q1CommanderBundle,
) -> Mapping[str, object] | None:
    timeout_seconds = float(
        bundle.transport_config.transport_timeout_seconds
    )
    poll_interval_seconds = float(
        bundle.transport_config.transport_poll_interval_seconds
    )
    deadline = time.monotonic() + timeout_seconds
    while True:
        output = _load_output(bundle.output_file)
        if output is not None:
            return output
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(poll_interval_seconds, remaining))


def _validate_and_normalize_request(
    request: Mapping[str, object],
) -> dict[str, Any]:
    if set(request) != set(_EXPECTED_REQUEST_KEYS):
        raise ValueError("Q1 review request fields differ from the versioned schema")
    if request.get("schema_version") != Q1_LLM_REVIEW_REQUEST_SCHEMA_VERSION:
        raise ValueError("Unsupported Q1 review request schema")
    request_id = request.get("request_id")
    if (
        not isinstance(request_id, str)
        or not request_id
        or len(request_id) > _REQUEST_ID_MAX_LENGTH
    ):
        raise ValueError("Invalid Q1 review request_id")
    context_hash = request.get("context_manifest_hash")
    if (
        not isinstance(context_hash, str)
        or len(context_hash) != _HASH_LENGTH
        or any(character not in "0123456789abcdef" for character in context_hash)
    ):
        raise ValueError("Invalid Q1 review context_manifest_hash")
    if request.get("real_order_routing") is not False:
        raise ValueError("Q1 commander requests must disable real order routing")
    raw_evidence = request.get("allowed_evidence_event_ids")
    evidence = (
        cast(list[object], raw_evidence)
        if isinstance(raw_evidence, list)
        else None
    )
    if (
        evidence is None
        or len(evidence) > 100
        or any(not isinstance(event_id, str) or not event_id for event_id in evidence)
        or len(set(cast(list[str], evidence))) != len(evidence)
    ):
        raise ValueError("Invalid bounded Q1 evidence IDs")
    raw_allowed_outputs = request.get("allowed_outputs")
    allowed_outputs = (
        cast(Mapping[object, object], raw_allowed_outputs)
        if isinstance(raw_allowed_outputs, Mapping)
        else None
    )
    if allowed_outputs is None or dict(allowed_outputs) != {
        "risk_multiplier": [1.0, 0.75, 0.5],
        "block_new_entries": "boolean",
        "new_symbols": [],
        "order_quantities": "forbidden",
        "broker_actions": "forbidden",
    }:
        raise ValueError("Q1 request output boundary differs from its schema")
    _reject_sensitive_keys(request)
    normalized = canonical_data(request)
    if not isinstance(normalized, dict):
        raise ValueError("Q1 review request must be an object")
    return cast(dict[str, Any], normalized)


def _validate_output(
    raw_output: Mapping[str, object] | Q1LlmOverlayDecision,
    *,
    request: Mapping[str, object],
) -> Q1LlmOverlayDecision:
    decision = (
        raw_output
        if isinstance(raw_output, Q1LlmOverlayDecision)
        else Q1LlmOverlayDecision.model_validate(dict(raw_output))
    )
    if decision.request_id != request["request_id"]:
        raise ValueError("Q1 commander output request_id mismatch")
    if decision.context_manifest_hash != request["context_manifest_hash"]:
        raise ValueError("Q1 commander output context hash mismatch")
    evidence = cast(list[str], request["allowed_evidence_event_ids"])
    validate_bounded_evidence(
        decision,
        allowed_event_ids=set(evidence),
    )
    return decision


def _load_output(path: Path) -> Mapping[str, object] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return cast(dict[str, object], payload)


def _same_selection(
    expected: SelectionSnapshot,
    current: SelectionSnapshot | None,
) -> bool:
    return (
        current is not None
        and current.selection_id == expected.selection_id
        and current.version == expected.version
        and current.provider is expected.provider
        and current.model == expected.model
        and current.reasoning_profile == expected.reasoning_profile
        and current.config_hash == expected.config_hash
    )


def _audit_request_id(request: Mapping[str, object]) -> str:
    value = request.get("request_id")
    if (
        isinstance(value, str)
        and value
        and len(value) <= _REQUEST_ID_MAX_LENGTH
    ):
        return value
    return "__invalid_request__"


def _validated_output_hash(decision: Q1LlmOverlayDecision) -> str:
    return canonical_hash(decision.model_dump(mode="json"))


def _reject_sensitive_keys(value: object) -> None:
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        for raw_key, child in mapping.items():
            key = str(raw_key).strip().lower().replace("-", "_")
            if key in _SENSITIVE_KEY_TOKENS or any(
                fragment in key for fragment in _SENSITIVE_KEY_FRAGMENTS
            ):
                raise ValueError("Q1 bundle request contains a sensitive field")
            _reject_sensitive_keys(child)
        return
    if isinstance(value, list | tuple):
        values = cast(list[object] | tuple[object, ...], value)
        for child in values:
            _reject_sensitive_keys(child)


def _build_prompt(
    *,
    selection: SelectionSnapshot,
    bundle_hash: str,
) -> str:
    return f"""# Q1 reduce-only commander review

Provider: {selection.provider.value}
Model: {selection.model}
Reasoning profile: {selection.reasoning_profile}
Selection version: {selection.version}
Bundle hash: {bundle_hash}

Read `request.json`. Return exactly one JSON object matching
`output.schema.json`. The only permitted policy controls are
`risk_multiplier` in `1.00`, `0.75`, `0.50` and `block_new_entries`.
Use only evidence IDs present in `allowed_evidence_event_ids`.

You may reduce or preserve Q1-DET risky exposure. You may not increase risk,
choose order quantities or broker actions, add symbols, route real orders, or
override deterministic HARD_REDUCE/CRITICAL_EXIT controls. If evidence does not
justify a reduction, return multiplier `1.00` and `block_new_entries=false`.
"""


def _atomic_write_utf8(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        dir=path.parent,
        delete=False,
    ) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    temporary.replace(path)


def _codex_executable() -> str:
    executable_name = "codex.cmd" if os.name == "nt" else "codex"
    return shutil.which(executable_name) or executable_name
