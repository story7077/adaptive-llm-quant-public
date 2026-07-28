from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel

from trading.research.oos_v2 import (
    OosLockboxResultV2,
    OosWorkerRequestV2,
    OosWorkerResponseV2,
    PrivateOosDatasetManifestV2,
)
from trading.research.portfolio_delta_sharpe import (
    PortfolioComparisonContractV1,
    PortfolioDeltaSharpeResultV1,
)
from trading.research.promotion_v2 import (
    PromotionEvaluationContractV2,
    PromotionEvidenceV2,
    TrustedPromotionEvaluationV2,
    TrustedShadowPerformanceSummaryV2,
)

SCHEMAS: dict[str, type[BaseModel]] = {
    "portfolio-comparison-contract-v1.schema.json": (
        PortfolioComparisonContractV1
    ),
    "portfolio-delta-sharpe-result-v1.schema.json": (
        PortfolioDeltaSharpeResultV1
    ),
    "private-oos-dataset-manifest-v2.schema.json": (
        PrivateOosDatasetManifestV2
    ),
    "oos-worker-request-v2.schema.json": OosWorkerRequestV2,
    "oos-lockbox-result-v2.schema.json": OosLockboxResultV2,
    "oos-worker-response-v2.schema.json": OosWorkerResponseV2,
    "trusted-shadow-performance-summary-v2.schema.json": (
        TrustedShadowPerformanceSummaryV2
    ),
    "promotion-evaluation-contract-v2.schema.json": (
        PromotionEvaluationContractV2
    ),
    "promotion-evidence-v2.schema.json": PromotionEvidenceV2,
    "trusted-promotion-evaluation-v2.schema.json": (
        TrustedPromotionEvaluationV2
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


def test_portfolio_sharpe_schemas_and_hash_manifest_are_canonical(
    repository_root: Path,
) -> None:
    root = repository_root / "contracts" / "portfolio-sharpe-v2"
    manifest = json.loads(
        (
            root / "portfolio-sharpe-schema-hashes-v2.json"
        ).read_text(encoding="utf-8")
    )
    assert manifest["schema_version"] == "portfolio_sharpe_schema_hashes_v2"
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
