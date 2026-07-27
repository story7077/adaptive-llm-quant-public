from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from trading.control.contracts import AdaptivePolicyDecision


def no_change_payload() -> dict[str, object]:
    return {
        "schema_version": "adaptive_policy_decision_v1",
        "request_id": "request-1",
        "context_manifest_hash": "a" * 64,
        "decision": "NO_CHANGE",
        "arm_scope": "B3-RISK",
        "base_policy_version": 0,
        "effective_from": None,
        "expires_at": None,
        "operations": [],
        "evidence_news_event_ids": [],
        "raw_confidence": 0.2,
        "rollback_conditions": [],
        "rationale_summary": "Evidence is insufficient.",
    }


def test_no_change_contract_round_trips() -> None:
    decision = AdaptivePolicyDecision.model_validate(no_change_payload())
    assert AdaptivePolicyDecision.model_validate_json(decision.model_dump_json()) == decision


def test_output_contract_rejects_order_fields() -> None:
    payload = no_change_payload()
    payload["orders"] = [{"symbol": "SOXL", "quantity": 100}]
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AdaptivePolicyDecision.model_validate(payload)


def test_apply_patch_requires_evidence_and_window() -> None:
    payload = no_change_payload()
    payload.update(
        {
            "decision": "APPLY_PATCH",
            "effective_from": datetime(2026, 7, 26, tzinfo=UTC),
            "expires_at": datetime(2026, 7, 26, tzinfo=UTC) + timedelta(hours=1),
            "operations": [
                {
                    "action": "REDUCE_RISK_BUDGET",
                    "target_kind": "PORTFOLIO",
                    "target_id": "TOTAL",
                    "risk_budget_delta": None,
                    "risk_multiplier": 0.75,
                    "blocked": None,
                }
            ],
        }
    )
    with pytest.raises(ValidationError, match="news-event evidence"):
        AdaptivePolicyDecision.model_validate(payload)
