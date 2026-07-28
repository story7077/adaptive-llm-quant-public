from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trading.control.contracts import AdaptivePolicyDecision, CommanderRequest
from trading.control.providers import CommanderProvider
from trading.control.service import build_decision_prompt
from trading.domain.hashing import canonical_json


@dataclass(frozen=True, slots=True)
class ExportedRequestBundle:
    directory: Path
    request_file: Path
    schema_file: Path
    prompt_file: Path
    output_file: Path
    codex_command: tuple[str, ...] | None

    def as_payload(self) -> dict[str, Any]:
        return {
            "directory": str(self.directory),
            "request_file": str(self.request_file),
            "schema_file": str(self.schema_file),
            "prompt_file": str(self.prompt_file),
            "output_file": str(self.output_file),
            "codex_command": (
                None if self.codex_command is None else list(self.codex_command)
            ),
        }


def export_request_bundle(
    request: CommanderRequest,
    *,
    commander_dir: Path,
) -> ExportedRequestBundle:
    target = commander_dir / "inbox" / request.request_id
    target.mkdir(parents=True, exist_ok=True)
    request_file = target / "request.json"
    schema_file = target / "output.schema.json"
    prompt_file = target / "prompt.md"
    output_file = target / "output.json"
    prompt = build_decision_prompt(
        request_id=request.request_id,
        provider=request.provider,
        scope_id=request.scope_id,
        arm_scope=request.arm_scope,
        base_policy_version=request.base_policy_version,
        context_manifest_hash=request.context_manifest_hash,
    )
    _atomic_write_utf8(
        request_file,
        json.dumps(request.model_dump(mode="json"), ensure_ascii=False, indent=2),
    )
    _atomic_write_utf8(
        schema_file,
        json.dumps(
            AdaptivePolicyDecision.model_json_schema(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
    )
    _atomic_write_utf8(prompt_file, prompt)

    command: tuple[str, ...] | None = None
    if request.provider is CommanderProvider.CODEX_SOL_MAX:
        command = (
            _codex_executable(),
            "exec",
            "--cd",
            str(commander_dir),
            "--skip-git-repo-check",
            "--ephemeral",
            "--ignore-user-config",
            "--strict-config",
            "--model",
            "gpt-5.6-sol",
            "-c",
            'model_reasoning_effort="max"',
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
    return ExportedRequestBundle(
        directory=target,
        request_file=request_file,
        schema_file=schema_file,
        prompt_file=prompt_file,
        output_file=output_file,
        codex_command=command,
    )


def run_codex_bundle(
    bundle: ExportedRequestBundle,
    *,
    timeout_seconds: int = 900,
) -> dict[str, Any]:
    if bundle.codex_command is None:
        raise ValueError("This request was not prepared for Codex Sol Max")
    request_payload = json.loads(bundle.request_file.read_text(encoding="utf-8"))
    prompt = (
        bundle.prompt_file.read_text(encoding="utf-8")
        + "\n\nPrepared request JSON:\n"
        + canonical_json(request_payload)
    )
    completed = subprocess.run(
        bundle.codex_command,
        cwd=bundle.directory.parents[1],
        input=prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout_seconds,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"Codex request failed ({completed.returncode}): {detail}")
    return json.loads(bundle.output_file.read_text(encoding="utf-8"))


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
        temp_path = Path(handle.name)
    temp_path.replace(path)


def _codex_executable() -> str:
    executable_name = "codex.cmd" if os.name == "nt" else "codex"
    return shutil.which(executable_name) or executable_name
