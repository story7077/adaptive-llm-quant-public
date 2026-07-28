from __future__ import annotations

import hashlib
import json
from pathlib import Path

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

SCHEMAS: dict[str, type[BaseModel]] = {
    "meta-oos-commander-binding-v1.schema.json": MetaOosCommanderBindingV1,
    "meta-oos-commander-invocation-v1.schema.json": (
        MetaOosCommanderInvocationV1
    ),
    "meta-oos-epoch-v1.schema.json": MetaOosEpochV1,
    "meta-oos-evaluation-contract-v1.schema.json": (
        MetaOosEvaluationContractV1
    ),
    "chronological-meta-oos-plan-v1.schema.json": (
        ChronologicalMetaOosPlanV1
    ),
    "meta-oos-candidate-availability-v1.schema.json": (
        MetaOosCandidateAvailabilityV1
    ),
    "meta-oos-epoch-context-v1.schema.json": MetaOosEpochContextV1,
    "meta-oos-memory-snapshot-v1.schema.json": MetaOosMemorySnapshotV1,
    "meta-oos-policy-decision-v1.schema.json": MetaOosPolicyDecisionV1,
    "meta-oos-outer-audit-reservation-v1.schema.json": (
        MetaOosOuterAuditReservationV1
    ),
    "meta-oos-epoch-arm-audit-record-v1.schema.json": (
        MetaOosEpochArmAuditRecordV1
    ),
    "meta-oos-arm-aggregate-v1.schema.json": MetaOosArmAggregateV1,
    "meta-oos-paired-comparison-v1.schema.json": (
        MetaOosPairedComparisonV1
    ),
    "chronological-meta-oos-result-v1.schema.json": (
        ChronologicalMetaOosResultV1
    ),
}


def _expected_bytes(filename: str, model: type[BaseModel]) -> bytes:
    document = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": (
            "https://adaptive-llm-quant.example/schemas/"
            f"{filename}"
        ),
        **model.model_json_schema(),
    }
    return (
        json.dumps(
            document,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def test_meta_oos_schemas_and_hash_manifest_are_canonical(
    repository_root: Path,
) -> None:
    root = repository_root / "contracts" / "meta-oos-v1"
    manifest = json.loads(
        (root / "meta-oos-schema-hashes-v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["schema_version"] == "meta_oos_schema_hashes_v1"
    assert manifest["canonical_source"] == "adaptive-llm-quant-public"
    assert set(manifest["schemas"]) == set(SCHEMAS)

    for filename, model in SCHEMAS.items():
        actual = (root / filename).read_bytes()
        expected = _expected_bytes(filename, model)
        assert not actual.startswith(b"\xef\xbb\xbf")
        assert actual == expected
        assert manifest["schemas"][filename] == hashlib.sha256(
            actual
        ).hexdigest()
