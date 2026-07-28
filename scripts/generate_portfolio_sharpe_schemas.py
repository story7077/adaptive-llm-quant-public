from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

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

SCHEMAS: tuple[tuple[str, type[BaseModel]], ...] = (
    (
        "portfolio-comparison-contract-v1.schema.json",
        PortfolioComparisonContractV1,
    ),
    (
        "portfolio-delta-sharpe-result-v1.schema.json",
        PortfolioDeltaSharpeResultV1,
    ),
    ("private-oos-dataset-manifest-v2.schema.json", PrivateOosDatasetManifestV2),
    ("oos-worker-request-v2.schema.json", OosWorkerRequestV2),
    ("oos-lockbox-result-v2.schema.json", OosLockboxResultV2),
    ("oos-worker-response-v2.schema.json", OosWorkerResponseV2),
    (
        "trusted-shadow-performance-summary-v2.schema.json",
        TrustedShadowPerformanceSummaryV2,
    ),
    (
        "promotion-evaluation-contract-v2.schema.json",
        PromotionEvaluationContractV2,
    ),
    ("promotion-evidence-v2.schema.json", PromotionEvidenceV2),
    (
        "trusted-promotion-evaluation-v2.schema.json",
        TrustedPromotionEvaluationV2,
    ),
)
MANIFEST_FILENAME = "portfolio-sharpe-schema-hashes-v2.json"


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
        "schema_version": "portfolio_sharpe_schema_hashes_v2",
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
