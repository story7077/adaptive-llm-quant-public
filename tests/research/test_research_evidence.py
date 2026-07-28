from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from trading.domain.hashing import canonical_hash
from trading.research.evidence import (
    ResearchEvidenceBundleV1,
    research_source_content_hash,
)

AS_OF = datetime(2026, 7, 27, 21, 0, tzinfo=UTC)


def source_payload(
    *,
    source_id: str = "sec-aapl-10q",
    publisher: str = "SEC",
    source_tier: str = "TIER_1_OFFICIAL",
    corroborated: bool = True,
    contradiction: bool = False,
) -> dict[str, object]:
    published_at = AS_OF - timedelta(hours=4)
    first_available_at = AS_OF - timedelta(hours=3, minutes=59)
    excerpt = "The issuer reported a bounded, point-in-time operating result."
    url = f"https://example.test/{source_id}"
    title = f"Source {source_id}"
    return {
        "source_id": source_id,
        "url": url,
        "title": title,
        "publisher": publisher,
        "published_at": published_at,
        "first_available_at": first_available_at,
        "captured_at": AS_OF + timedelta(minutes=2),
        "source_tier": source_tier,
        "content_hash": research_source_content_hash(
            url=url,
            title=title,
            publisher=publisher,
            published_at=published_at,
            first_available_at=first_available_at,
            excerpt=excerpt,
        ),
        "excerpt": excerpt,
        "license_note": "Short factual excerpt retained for research provenance.",
        "instrument_tags": ["AAPL"],
        "factor_tags": ["quality"],
        "corroborated": corroborated,
        "contradiction": contradiction,
    }


def bundle_payload() -> dict[str, object]:
    source = source_payload()
    return {
        "schema_version": "research_evidence_bundle_v1",
        "request_id": "request-001",
        "research_cycle_id": "cycle-001",
        "role": "WEB_SCOUT",
        "context_manifest_hash": canonical_hash({"context": 1}),
        "available_data_catalog_hash": canonical_hash({"catalog": 1}),
        "model_family": "GPT-5.6 Sol Pro",
        "reasoning_profile": "xhigh",
        "browser_session_id": "browser-001",
        "conversation_id": "conversation-001",
        "agbrowse_request_id": "agbrowse-001",
        "as_of": AS_OF,
        "data_available_cutoff": AS_OF,
        "captured_at": AS_OF + timedelta(minutes=3),
        "queries": [
            {
                "query_id": "query-001",
                "purpose": "DISCOVER_ALPHA",
                "query": "Find primary evidence for persistent quality effects.",
                "started_at": AS_OF,
                "completed_at": AS_OF + timedelta(minutes=1),
                "status": "COMPLETED",
                "source_ids": ["sec-aapl-10q"],
                "instrument_scope": ["AAPL"],
                "factor_scope": ["quality"],
            }
        ],
        "sources": [source],
        "claims": [
            {
                "claim_id": "claim-001",
                "claim_kind": "ECONOMIC_MECHANISM",
                "statement": "Official issuer data supports a testable quality mechanism.",
                "verification_status": "CORROBORATED",
                "source_ids": ["sec-aapl-10q"],
                "instrument_tags": ["AAPL"],
                "factor_tags": ["quality"],
                "falsification_test": "Neutralize quality exposure and rerun OOS.",
            }
        ],
        "unresolved_questions": [],
    }


def test_official_evidence_supports_corroborated_research_claim() -> None:
    bundle = ResearchEvidenceBundleV1.model_validate(bundle_payload())

    assert bundle.sources[0].source_tier.value == "TIER_1_OFFICIAL"
    assert bundle.claims[0].falsification_test is not None


def test_social_only_source_cannot_be_promoted_to_fact() -> None:
    payload = bundle_payload()
    social = source_payload(
        source_id="reddit-rumor",
        publisher="Reddit",
        source_tier="TIER_5_SOCIAL",
        corroborated=False,
    )
    payload["sources"] = [social]
    query = payload["queries"][0]  # type: ignore[index]
    assert isinstance(query, dict)
    query["source_ids"] = ["reddit-rumor"]
    claim = payload["claims"][0]  # type: ignore[index]
    assert isinstance(claim, dict)
    claim["source_ids"] = ["reddit-rumor"]

    with pytest.raises(ValidationError, match=r"social-only|corroboration"):
        ResearchEvidenceBundleV1.model_validate(payload)

    claim["verification_status"] = "UNVERIFIED"
    bundle = ResearchEvidenceBundleV1.model_validate(payload)
    assert bundle.claims[0].verification_status.value == "UNVERIFIED"


def test_source_hash_and_point_in_time_cutoff_are_fail_closed() -> None:
    bad_hash = bundle_payload()
    source = bad_hash["sources"][0]  # type: ignore[index]
    assert isinstance(source, dict)
    source["content_hash"] = "0" * 64
    with pytest.raises(ValidationError, match="content_hash"):
        ResearchEvidenceBundleV1.model_validate(bad_hash)

    future_source = bundle_payload()
    source = future_source["sources"][0]  # type: ignore[index]
    assert isinstance(source, dict)
    source["first_available_at"] = AS_OF + timedelta(seconds=1)
    source["captured_at"] = AS_OF + timedelta(minutes=2)
    source["content_hash"] = research_source_content_hash(
        url=str(source["url"]),
        title=str(source["title"]),
        publisher=str(source["publisher"]),
        published_at=source["published_at"],  # type: ignore[arg-type]
        first_available_at=source["first_available_at"],  # type: ignore[arg-type]
        excerpt=str(source["excerpt"]),
    )
    with pytest.raises(ValidationError, match="unavailable at the evidence cutoff"):
        ResearchEvidenceBundleV1.model_validate(future_source)


def test_every_source_must_come_from_an_active_browse_query() -> None:
    payload = bundle_payload()
    second = source_payload(source_id="fred-series", publisher="Federal Reserve")
    payload["sources"] = [payload["sources"][0], second]  # type: ignore[index]

    with pytest.raises(ValidationError, match="attributed to a browse query"):
        ResearchEvidenceBundleV1.model_validate(payload)
