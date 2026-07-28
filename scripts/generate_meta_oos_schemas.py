from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from trading.research.chronological_meta_oos import (
    ChronologicalMetaOosPlanV1,
    ChronologicalMetaOosResultV1,
    MetaOosArmAggregateV1,
    MetaOosCandidateAvailabilityV1,
    MetaOosCommanderBindingV1,
    MetaOosCommanderInvocationV1,
    MetaOosEpochArmAuditRecordV1,
    MetaOosEpochContextV1,
    MetaOosEpochV1,
    MetaOosEvaluationContractV1,
    MetaOosMemorySnapshotV1,
    MetaOosOuterAuditReservationV1,
    MetaOosPairedComparisonV1,
    MetaOosPolicyDecisionV1,
)

SCHEMAS: tuple[tuple[str, type[BaseModel]], ...] = (
    ("meta-oos-commander-binding-v1.schema.json", MetaOosCommanderBindingV1),
    (
        "meta-oos-commander-invocation-v1.schema.json",
        MetaOosCommanderInvocationV1,
    ),
    ("meta-oos-epoch-v1.schema.json", MetaOosEpochV1),
    (
        "meta-oos-evaluation-contract-v1.schema.json",
        MetaOosEvaluationContractV1,
    ),
    ("chronological-meta-oos-plan-v1.schema.json", ChronologicalMetaOosPlanV1),
    (
        "meta-oos-candidate-availability-v1.schema.json",
        MetaOosCandidateAvailabilityV1,
    ),
    ("meta-oos-epoch-context-v1.schema.json", MetaOosEpochContextV1),
    ("meta-oos-memory-snapshot-v1.schema.json", MetaOosMemorySnapshotV1),
    ("meta-oos-policy-decision-v1.schema.json", MetaOosPolicyDecisionV1),
    (
        "meta-oos-outer-audit-reservation-v1.schema.json",
        MetaOosOuterAuditReservationV1,
    ),
    (
        "meta-oos-epoch-arm-audit-record-v1.schema.json",
        MetaOosEpochArmAuditRecordV1,
    ),
    ("meta-oos-arm-aggregate-v1.schema.json", MetaOosArmAggregateV1),
    ("meta-oos-paired-comparison-v1.schema.json", MetaOosPairedComparisonV1),
    (
        "chronological-meta-oos-result-v1.schema.json",
        ChronologicalMetaOosResultV1,
    ),
)
MANIFEST_FILENAME = "meta-oos-schema-hashes-v1.json"


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
        "schema_version": "meta_oos_schema_hashes_v1",
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
