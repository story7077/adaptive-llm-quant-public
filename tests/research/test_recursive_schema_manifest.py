from __future__ import annotations

import hashlib
import json
from pathlib import Path

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

SCHEMAS: dict[str, type[BaseModel]] = {
    "research-memory-snapshot-v1.schema.json": ResearchMemorySnapshotV1,
    "research-action-plan-v1.schema.json": ResearchActionPlanV1,
    "algorithm-proposal-v2.schema.json": AlgorithmProposalV2,
    "research-request-v2.schema.json": ResearchRequestV2,
    "research-decision-v2.schema.json": ResearchDecisionV2,
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


def test_recursive_contract_schemas_and_hash_manifest_are_canonical(
    repository_root: Path,
) -> None:
    root = repository_root / "contracts" / "research-v2"
    manifest = json.loads(
        (
            root / "recursive-contract-schema-hashes-v1.json"
        ).read_text(encoding="utf-8")
    )
    assert manifest["schema_version"] == (
        "recursive_contract_schema_hashes_v1"
    )
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
