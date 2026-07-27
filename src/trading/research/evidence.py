from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Literal, Self
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator

from trading.domain.contracts import DomainModel
from trading.domain.hashing import canonical_hash
from trading.domain.time import require_aware_utc

EVIDENCE_BUNDLE_SCHEMA_VERSION = "research_evidence_bundle_v1"
EXPECTED_WEBGPT_MODEL = "GPT-5.6 Sol Pro"
EXPECTED_WEBGPT_REASONING = "xhigh"
SHA256_PATTERN = r"^[a-f0-9]{64}$"
IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$"
SYMBOL_PATTERN = r"^[A-Z0-9][A-Z0-9._-]{0,15}$"


class SourceTier(StrEnum):
    TIER_1_OFFICIAL = "TIER_1_OFFICIAL"
    TIER_2_PRIMARY_DATA = "TIER_2_PRIMARY_DATA"
    TIER_3_REPUTABLE_NEWS = "TIER_3_REPUTABLE_NEWS"
    TIER_4_INDUSTRY_ANALYSIS = "TIER_4_INDUSTRY_ANALYSIS"
    TIER_5_SOCIAL = "TIER_5_SOCIAL"
    TIER_6_UNVERIFIED = "TIER_6_UNVERIFIED"


class EvidencePurpose(StrEnum):
    DISCOVER_ALPHA = "DISCOVER_ALPHA"
    EXPLAIN_STRATEGY_FAILURE = "EXPLAIN_STRATEGY_FAILURE"
    FALSIFY_HYPOTHESIS = "FALSIFY_HYPOTHESIS"
    ECONOMIC_MECHANISM = "ECONOMIC_MECHANISM"
    FACTOR_OR_REGIME = "FACTOR_OR_REGIME"
    EXECUTION_COST_OR_CAPACITY = "EXECUTION_COST_OR_CAPACITY"
    DATA_DILIGENCE = "DATA_DILIGENCE"


class ClaimKind(StrEnum):
    OBSERVATION = "OBSERVATION"
    ECONOMIC_MECHANISM = "ECONOMIC_MECHANISM"
    STRATEGY_FAILURE_EVIDENCE = "STRATEGY_FAILURE_EVIDENCE"
    FACTOR_OR_REGIME = "FACTOR_OR_REGIME"
    EXECUTION_OR_CAPACITY = "EXECUTION_OR_CAPACITY"
    DATA_CONSTRAINT = "DATA_CONSTRAINT"
    FALSIFICATION_LEAD = "FALSIFICATION_LEAD"


class VerificationStatus(StrEnum):
    CORROBORATED = "CORROBORATED"
    UNVERIFIED = "UNVERIFIED"
    CONTRADICTED = "CONTRADICTED"


class BrowseStatus(StrEnum):
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class ResearchSourceRecord(DomainModel):
    source_id: str = Field(pattern=IDENTIFIER_PATTERN)
    url: str = Field(min_length=8, max_length=2048)
    title: str = Field(min_length=1, max_length=600)
    publisher: str = Field(min_length=1, max_length=200)
    published_at: datetime
    first_available_at: datetime
    captured_at: datetime
    source_tier: SourceTier
    content_hash: str = Field(pattern=SHA256_PATTERN)
    excerpt: str = Field(min_length=1, max_length=1200)
    license_note: str = Field(min_length=1, max_length=500)
    instrument_tags: list[str] = Field(default_factory=list, max_length=128)
    factor_tags: list[str] = Field(default_factory=list, max_length=64)
    corroborated: bool
    contradiction: bool

    @field_validator(
        "published_at",
        "first_available_at",
        "captured_at",
        mode="after",
    )
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @field_validator("url", mode="after")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("url must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password:
            raise ValueError("url must not contain credentials")
        return value

    @field_validator("instrument_tags", mode="after")
    @classmethod
    def normalize_instruments(cls, value: list[str]) -> list[str]:
        normalized = [item.strip().upper() for item in value]
        if any(re.fullmatch(SYMBOL_PATTERN, item) is None for item in normalized):
            raise ValueError("instrument_tags must contain market symbols")
        _require_unique(normalized, "instrument_tags")
        return normalized

    @field_validator("factor_tags", mode="after")
    @classmethod
    def normalize_factors(cls, value: list[str]) -> list[str]:
        normalized = [item.strip().lower() for item in value]
        if any(not item or len(item) > 80 for item in normalized):
            raise ValueError("factor_tags values must contain 1 to 80 characters")
        _require_unique(normalized, "factor_tags")
        return normalized

    @model_validator(mode="after")
    def validate_provenance(self) -> Self:
        if self.first_available_at < self.published_at:
            raise ValueError("first_available_at must not precede published_at")
        if self.captured_at < self.first_available_at:
            raise ValueError("captured_at must not precede first_available_at")
        expected_hash = research_source_content_hash(
            url=self.url,
            title=self.title,
            publisher=self.publisher,
            published_at=self.published_at,
            first_available_at=self.first_available_at,
            excerpt=self.excerpt,
        )
        if self.content_hash != expected_hash:
            raise ValueError("content_hash does not match the captured evidence fields")
        return self


class BrowseQueryRecord(DomainModel):
    query_id: str = Field(pattern=IDENTIFIER_PATTERN)
    purpose: EvidencePurpose
    query: str = Field(min_length=3, max_length=800)
    started_at: datetime
    completed_at: datetime
    status: BrowseStatus
    source_ids: list[str] = Field(default_factory=list, max_length=64)
    instrument_scope: list[str] = Field(default_factory=list, max_length=128)
    factor_scope: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("started_at", "completed_at", mode="after")
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @field_validator("source_ids", mode="after")
    @classmethod
    def validate_source_ids(cls, value: list[str]) -> list[str]:
        _require_unique(value, "source_ids")
        return value

    @field_validator("instrument_scope", mode="after")
    @classmethod
    def normalize_instrument_scope(cls, value: list[str]) -> list[str]:
        normalized = [item.strip().upper() for item in value]
        if any(re.fullmatch(SYMBOL_PATTERN, item) is None for item in normalized):
            raise ValueError("instrument_scope must contain market symbols")
        _require_unique(normalized, "instrument_scope")
        return normalized

    @field_validator("factor_scope", mode="after")
    @classmethod
    def normalize_factor_scope(cls, value: list[str]) -> list[str]:
        normalized = [item.strip().lower() for item in value]
        if any(not item or len(item) > 80 for item in normalized):
            raise ValueError("factor_scope values must contain 1 to 80 characters")
        _require_unique(normalized, "factor_scope")
        return normalized

    @model_validator(mode="after")
    def validate_query(self) -> Self:
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must not precede started_at")
        if self.status is BrowseStatus.COMPLETED and not self.source_ids:
            raise ValueError("completed browse queries must cite at least one source")
        return self


class ResearchClaim(DomainModel):
    claim_id: str = Field(pattern=IDENTIFIER_PATTERN)
    claim_kind: ClaimKind
    statement: str = Field(min_length=1, max_length=1600)
    verification_status: VerificationStatus
    source_ids: list[str] = Field(min_length=1, max_length=32)
    instrument_tags: list[str] = Field(default_factory=list, max_length=128)
    factor_tags: list[str] = Field(default_factory=list, max_length=64)
    falsification_test: str | None = Field(default=None, max_length=1200)

    @field_validator("source_ids", mode="after")
    @classmethod
    def validate_source_ids(cls, value: list[str]) -> list[str]:
        _require_unique(value, "source_ids")
        return value

    @field_validator("instrument_tags", mode="after")
    @classmethod
    def normalize_instruments(cls, value: list[str]) -> list[str]:
        normalized = [item.strip().upper() for item in value]
        if any(re.fullmatch(SYMBOL_PATTERN, item) is None for item in normalized):
            raise ValueError("instrument_tags must contain market symbols")
        _require_unique(normalized, "instrument_tags")
        return normalized

    @field_validator("factor_tags", mode="after")
    @classmethod
    def normalize_factors(cls, value: list[str]) -> list[str]:
        normalized = [item.strip().lower() for item in value]
        if any(not item or len(item) > 80 for item in normalized):
            raise ValueError("factor_tags values must contain 1 to 80 characters")
        _require_unique(normalized, "factor_tags")
        return normalized


class ResearchEvidenceBundleV1(DomainModel):
    schema_version: Literal["research_evidence_bundle_v1"] = EVIDENCE_BUNDLE_SCHEMA_VERSION
    request_id: str = Field(pattern=IDENTIFIER_PATTERN)
    research_cycle_id: str = Field(pattern=IDENTIFIER_PATTERN)
    role: Literal["WEB_SCOUT"] = "WEB_SCOUT"
    context_manifest_hash: str = Field(pattern=SHA256_PATTERN)
    available_data_catalog_hash: str = Field(pattern=SHA256_PATTERN)
    model_family: Literal["GPT-5.6 Sol Pro"] = EXPECTED_WEBGPT_MODEL
    reasoning_profile: Literal["xhigh"] = EXPECTED_WEBGPT_REASONING
    browser_session_id: str = Field(pattern=IDENTIFIER_PATTERN)
    conversation_id: str = Field(pattern=IDENTIFIER_PATTERN)
    agbrowse_request_id: str = Field(pattern=IDENTIFIER_PATTERN)
    as_of: datetime
    data_available_cutoff: datetime
    captured_at: datetime
    queries: list[BrowseQueryRecord] = Field(min_length=1, max_length=64)
    sources: list[ResearchSourceRecord] = Field(min_length=1, max_length=512)
    claims: list[ResearchClaim] = Field(
        default_factory=lambda: list[ResearchClaim](),
        max_length=256,
    )
    unresolved_questions: list[str] = Field(
        default_factory=lambda: list[str](),
        max_length=64,
    )

    @field_validator("as_of", "data_available_cutoff", "captured_at", mode="after")
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @field_validator("unresolved_questions", mode="after")
    @classmethod
    def validate_unresolved_questions(cls, value: list[str]) -> list[str]:
        _require_unique(value, "unresolved_questions")
        return value

    @model_validator(mode="after")
    def validate_bundle(self) -> Self:
        if self.data_available_cutoff > self.as_of:
            raise ValueError("data_available_cutoff must not exceed as_of")
        if self.captured_at < self.as_of:
            raise ValueError("captured_at must not precede as_of")

        source_by_id = {source.source_id: source for source in self.sources}
        if len(source_by_id) != len(self.sources):
            raise ValueError("source_id values must be unique")
        query_ids = [query.query_id for query in self.queries]
        _require_unique(query_ids, "query_id values")
        claim_ids = [claim.claim_id for claim in self.claims]
        _require_unique(claim_ids, "claim_id values")

        known_sources = set(source_by_id)
        if not any(query.status is BrowseStatus.COMPLETED for query in self.queries):
            raise ValueError("evidence bundle requires an actively completed browse query")
        queried_sources: set[str] = set()
        for source in self.sources:
            if source.first_available_at > self.data_available_cutoff:
                raise ValueError(
                    f"source {source.source_id} was unavailable at the evidence cutoff"
                )
            if source.captured_at > self.captured_at:
                raise ValueError(
                    f"source {source.source_id} was captured after the bundle timestamp"
                )

        for query in self.queries:
            if not set(query.source_ids).issubset(known_sources):
                raise ValueError(f"query {query.query_id} cites an unknown source_id")
            queried_sources.update(query.source_ids)
        if queried_sources != known_sources:
            raise ValueError("every evidence source must be attributed to a browse query")

        for claim in self.claims:
            if not set(claim.source_ids).issubset(known_sources):
                raise ValueError(f"claim {claim.claim_id} cites an unknown source_id")
            claim_sources = [source_by_id[source_id] for source_id in claim.source_ids]
            self._validate_claim_provenance(claim, claim_sources)

        for source in self.sources:
            if source.source_tier is not SourceTier.TIER_5_SOCIAL or not source.corroborated:
                continue
            if not any(
                claim.verification_status is VerificationStatus.CORROBORATED
                and source.source_id in claim.source_ids
                for claim in self.claims
            ):
                raise ValueError(
                    f"social source {source.source_id} cannot be marked corroborated alone"
                )
        return self

    @staticmethod
    def _validate_claim_provenance(
        claim: ResearchClaim,
        sources: list[ResearchSourceRecord],
    ) -> None:
        if claim.verification_status is VerificationStatus.CORROBORATED:
            has_official = any(
                source.source_tier is SourceTier.TIER_1_OFFICIAL for source in sources
            )
            independent_publishers = {
                source.publisher.casefold()
                for source in sources
                if source.source_tier
                not in {SourceTier.TIER_5_SOCIAL, SourceTier.TIER_6_UNVERIFIED}
            }
            has_independent_confirmation = len(independent_publishers) >= 2
            if not has_official and not has_independent_confirmation:
                raise ValueError(
                    f"claim {claim.claim_id} lacks official or independent corroboration"
                )
            if all(
                source.source_tier
                in {SourceTier.TIER_5_SOCIAL, SourceTier.TIER_6_UNVERIFIED}
                for source in sources
            ):
                raise ValueError(
                    f"claim {claim.claim_id} cannot treat social-only evidence as fact"
                )
        if (
            claim.verification_status is VerificationStatus.CONTRADICTED
            and not any(source.contradiction for source in sources)
        ):
            raise ValueError(
                f"claim {claim.claim_id} is contradicted without contradiction evidence"
            )


def research_source_content_hash(
    *,
    url: str,
    title: str,
    publisher: str,
    published_at: datetime,
    first_available_at: datetime,
    excerpt: str,
) -> str:
    return canonical_hash(
        {
            "url": url,
            "title": title,
            "publisher": publisher,
            "published_at": require_aware_utc(published_at),
            "first_available_at": require_aware_utc(first_available_at),
            "excerpt": excerpt,
        }
    )


def _require_unique(values: list[str], label: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"{label} must be unique")
