from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from trading.research.experiment_outcomes import (
    AlgorithmProposalV2,
    ResearchMemorySnapshotV1,
)
from trading.research.meta_controller import ResearchActionPlanV1
from trading.research.v2_contracts import (
    ResearchDecisionV2,
    ResearchRequestV2,
)

SCHEMAS: tuple[tuple[str, type[BaseModel]], ...] = (
    ("research-memory-snapshot-v1.schema.json", ResearchMemorySnapshotV1),
    ("research-action-plan-v1.schema.json", ResearchActionPlanV1),
    ("algorithm-proposal-v2.schema.json", AlgorithmProposalV2),
    ("research-request-v2.schema.json", ResearchRequestV2),
    ("research-decision-v2.schema.json", ResearchDecisionV2),
)
MANIFEST_FILENAME = "recursive-contract-schema-hashes-v1.json"


def _encoded_schema(
    filename: str,
    model: type[BaseModel],
) -> bytes:
    schema: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": (
            "https://adaptive-llm-quant.example/schemas/"
            f"{filename}"
        ),
        **model.model_json_schema(),
    }
    return (
        json.dumps(
            schema,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def generate(output_dir: Path) -> None:
    destination = output_dir.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    hashes: dict[str, str] = {}
    for filename, model in SCHEMAS:
        encoded = _encoded_schema(filename, model)
        (destination / filename).write_bytes(encoded)
        hashes[filename] = hashlib.sha256(encoded).hexdigest()
    manifest = {
        "schema_version": "recursive_contract_schema_hashes_v1",
        "canonical_source": "adaptive-llm-quant-public",
        "schemas": hashes,
    }
    (destination / MANIFEST_FILENAME).write_text(
        json.dumps(
            manifest,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    arguments = parser.parse_args()
    generate(arguments.output_dir)


if __name__ == "__main__":
    main()
