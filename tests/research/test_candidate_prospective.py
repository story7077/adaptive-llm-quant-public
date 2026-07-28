from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from math import exp, sin
from pathlib import Path

import pytest

from trading.data.q1_pit import AlignedDailyInputs, CompletedDailySeries
from trading.domain.hashing import canonical_hash
from trading.domain.q1 import (
    PointInTimeSourceReference,
    Q1ArmId,
    Q1DecisionInputManifest,
    Q1StrategyDecision,
    StrategyEvaluationAnchor,
)
from trading.research.candidate_abi import CandidateFeatureValueV1
from trading.research.candidate_artifact import (
    CandidateRequestBindingV1,
    CandidateRuntimeV1,
    build_candidate_artifact_bundle,
)
from trading.research.contracts import ResearchCommanderKind
from trading.research.prospective import (
    PriorProspectiveState,
    ProspectiveRequestEvidenceV1,
    build_prospective_request_evidence,
    load_prospective_candidate_config,
)
from trading.strategies.challengers.q1_det_v2_0_0.decision import decide

DECISION_TIME = datetime(2026, 7, 29, 14, 0, 5, tzinfo=UTC)
SESSION_DATES = tuple(
    date(2025, 12, 11) + timedelta(days=index) for index in range(200)
)


def _config(repository_root: Path):
    return load_prospective_candidate_config(repository_root / "config")


def _artifact():
    return build_candidate_artifact_bundle(
        bundle_id="candidate-bundle-prospective",
        challenger_id="challenger-prospective",
        request_binding=CandidateRequestBindingV1(
            request_id="research-request-prospective",
            research_cycle_id="research-cycle-prospective",
            context_manifest_hash="a" * 64,
            source_snapshot_commit="1" * 40,
            champion_version="1.0.0",
            experiment_family="prospective",
            selected_commander=ResearchCommanderKind.CODEX_SOL_MAX,
            commander_selection_id="selection-prospective",
            commander_selection_version=1,
        ),
        source_snapshot_hash="b" * 64,
        candidate_tree_hash="c" * 64,
        code_hash="d" * 64,
        config_hash="e" * 64,
        patch_hash="f" * 64,
        proposal_hash="1" * 64,
        builder_result_hash="2" * 64,
        test_manifest_hash="3" * 64,
        challenger_manifest_hash="4" * 64,
        validation_request_hash="5" * 64,
        runtime=CandidateRuntimeV1(
            implementation="CPython",
            version="3.13.12",
            abi_tag="cpython-313",
            executable_sha256="6" * 64,
        ),
        declared_entrypoint=(
            "trading.strategies.challengers.q1_det_v2_0_0.decision:decide"
        ),
    )


def _parent() -> Q1StrategyDecision:
    manifest = Q1DecisionInputManifest(
        config_manifest_hash="a" * 64,
        code_version="test-code",
        model_version="q1_math_signal_v1",
        source_manifest_hash="b" * 64,
        calendar_session_id="calendar-session-2026-07-29",
        source_bars=(
            PointInTimeSourceReference(
                record_id="parent-source-bar",
                available_at=DECISION_TIME - timedelta(days=1),
            ),
        ),
        quotes=(
            PointInTimeSourceReference(
                record_id="parent-quote",
                available_at=DECISION_TIME - timedelta(seconds=1),
            ),
        ),
        manifest_hash="c" * 64,
    )
    return Q1StrategyDecision(
        portfolio_decision_id="parent-q1-det-decision",
        run_id="parent-q1-run",
        arm_id=Q1ArmId.Q1_DET,
        source_cycle_id="parent-q1-cycle",
        input_state_sequence=1,
        decision_kind="NORMAL_REBALANCE",
        scheduled_at=DECISION_TIME - timedelta(seconds=5),
        signal_data_cutoff=DECISION_TIME - timedelta(seconds=5),
        portfolio_state_as_of=DECISION_TIME - timedelta(seconds=1),
        quote_as_of=DECISION_TIME - timedelta(seconds=1),
        decision_created_at=DECISION_TIME,
        valid_until=DECISION_TIME + timedelta(minutes=20),
        input_manifest=manifest,
        target_weights={
            "QQQ": Decimal("0.50"),
            "SOXX": Decimal("0.20"),
            "USD_CASH": Decimal("0.30"),
        },
        diagnostics={},
        worker_fence_token="test-fence",
        cycle_attempt_count=1,
        decision_hash="d" * 64,
        config_manifest_hash=manifest.config_manifest_hash,
        code_version=manifest.code_version,
        model_version=manifest.model_version,
        source_manifest_hash=manifest.source_manifest_hash,
    )


def _anchor() -> StrategyEvaluationAnchor:
    return StrategyEvaluationAnchor(
        evaluation_anchor_id="evaluation-anchor-prospective",
        run_id="parent-q1-run",
        calendar_session_id="calendar-session-2026-07-29",
        common_t0_at=DECISION_TIME - timedelta(minutes=1),
        initial_nav_usd=Decimal("100000"),
        quote_manifest_hash="e" * 64,
        anchor_hash="f" * 64,
        created_at=DECISION_TIME - timedelta(seconds=30),
        config_manifest_hash="a" * 64,
        code_version="test-code",
        model_version="q1_math_signal_v1",
        source_manifest_hash="b" * 64,
    )


def _inputs(scale: float = 1.0) -> AlignedDailyInputs:
    symbols = ("GLD", "QQQ", "SGOV", "SOXX", "TLT")
    multipliers = {
        "GLD": 0.45,
        "QQQ": 1.00,
        "SGOV": 0.05,
        "SOXX": 1.25,
        "TLT": -0.15,
    }
    series: dict[str, CompletedDailySeries] = {}
    for symbol in symbols:
        prices: list[Decimal] = []
        level = 100.0 * scale
        for index in range(len(SESSION_DATES)):
            periodic = 0.006 * sin(index / 3)
            level *= exp(0.0005 + multipliers[symbol] * periodic)
            prices.append(Decimal(str(level)))
        event_times = tuple(
            datetime.combine(value, datetime.min.time(), tzinfo=UTC)
            + timedelta(hours=20)
            for value in SESSION_DATES
        )
        available_ats = tuple(
            value + timedelta(minutes=1) for value in event_times
        )
        bar_ids = tuple(
            f"bar-{symbol.lower()}-{index:03d}"
            for index in range(len(SESSION_DATES))
        )
        payload_hashes = tuple(
            canonical_hash(
                {
                    "symbol": symbol,
                    "index": index,
                    "price": price,
                }
            )
            for index, price in enumerate(prices)
        )
        series[symbol] = CompletedDailySeries(
            symbol=symbol,
            session_dates=SESSION_DATES,
            adjusted_closes=tuple(prices),
            volumes=tuple(Decimal("1000000") for _ in SESSION_DATES),
            bar_ids=bar_ids,
            event_times=event_times,
            available_ats=available_ats,
            payload_hashes=payload_hashes,
        )
    return AlignedDailyInputs(
        session_dates=SESSION_DATES,
        series=series,
        source_bar_ids=tuple(
            sorted(bar_id for item in series.values() for bar_id in item.bar_ids)
        ),
        signal_data_cutoff=DECISION_TIME,
    )


def test_prospective_features_and_targets_are_price_scale_invariant(
    repository_root: Path,
) -> None:
    config = _config(repository_root)
    artifact = _artifact()
    first = build_prospective_request_evidence(
        config_bundle=config,
        artifact=artifact,
        parent_decision=_parent(),
        evaluation_anchor=_anchor(),
        market_inputs=_inputs(),
        prior_state=None,
    )
    scaled = build_prospective_request_evidence(
        config_bundle=config,
        artifact=artifact,
        parent_decision=_parent(),
        evaluation_anchor=_anchor(),
        market_inputs=_inputs(scale=17.0),
        prior_state=None,
    )

    first_features = {
        item.symbol: {feature.name: feature.value for feature in item.features}
        for item in first.request.instruments
    }
    scaled_features = {
        item.symbol: {feature.name: feature.value for feature in item.features}
        for item in scaled.request.instruments
    }
    for symbol in first_features:
        for name, value in first_features[symbol].items():
            assert scaled_features[symbol][name] == pytest.approx(
                value,
                rel=1e-12,
                abs=1e-12,
            )
    first_targets = decide(first.request.model_dump(mode="json"))["targets"]
    scaled_targets = decide(scaled.request.model_dump(mode="json"))["targets"]
    assert [
        (item["symbol"], item["target_weight"]) for item in scaled_targets
    ] == pytest.approx(
        [(item["symbol"], item["target_weight"]) for item in first_targets]
    )


def test_initial_request_is_cash_only_and_bound_to_completed_data(
    repository_root: Path,
) -> None:
    evidence = build_prospective_request_evidence(
        config_bundle=_config(repository_root),
        artifact=_artifact(),
        parent_decision=_parent(),
        evaluation_anchor=_anchor(),
        market_inputs=_inputs(),
        prior_state=None,
    )

    assert all(item.current_weight == 0 for item in evidence.request.instruments)
    assert evidence.source_manifest.state_source == (
        "CASH_ONLY_AT_EVALUATION_ANCHOR"
    )
    assert evidence.request.signal_data_cutoff == DECISION_TIME
    assert len(evidence.source_manifest.source_bars) == 1000
    assert evidence.source_manifest.completed_session_dates[-1] < date(
        2026,
        7,
        29,
    )
    assert all(
        item.available_at <= evidence.request.signal_data_cutoff
        for instrument in evidence.request.instruments
        for item in instrument.features
    )
    parent_targets = {
        instrument.symbol: next(
            feature.value
            for feature in instrument.features
            if feature.name == "parent_target_weight"
        )
        for instrument in evidence.request.instruments
    }
    assert parent_targets["QQQ"] == 0.5
    assert parent_targets["SOXX"] == 0.2
    assert parent_targets["GLD"] == 0


def test_prior_verified_targets_are_the_only_subsequent_state(
    repository_root: Path,
) -> None:
    prior = PriorProspectiveState(
        request_id="candidate-prospective-request-prior",
        execution_hash="9" * 64,
        target_weights={
            "GLD": 0.10,
            "QQQ": 0.50,
            "SGOV": 0.10,
            "SOXX": 0.20,
            "TLT": 0.05,
        },
        completed_sessions_since_review=7,
    )
    evidence = build_prospective_request_evidence(
        config_bundle=_config(repository_root),
        artifact=_artifact(),
        parent_decision=_parent(),
        evaluation_anchor=_anchor(),
        market_inputs=_inputs(),
        prior_state=prior,
    )

    assert {
        item.symbol: item.current_weight for item in evidence.request.instruments
    } == prior.target_weights
    assert evidence.source_manifest.prior_execution_hash == "9" * 64
    assert evidence.source_manifest.state_source == "PRIOR_VERIFIED_TARGETS"
    qqq = next(
        item for item in evidence.request.instruments if item.symbol == "QQQ"
    )
    review_clock = next(
        item
        for item in qqq.features
        if item.name == "completed_sessions_since_review"
    )
    assert review_clock.value == 7


def test_identical_versioned_inputs_replay_to_identical_hashes(
    repository_root: Path,
) -> None:
    kwargs = {
        "config_bundle": _config(repository_root),
        "artifact": _artifact(),
        "parent_decision": _parent(),
        "evaluation_anchor": _anchor(),
        "market_inputs": _inputs(),
        "prior_state": None,
    }
    first = build_prospective_request_evidence(**kwargs)
    second = build_prospective_request_evidence(**kwargs)
    assert first.evidence_hash == second.evidence_hash
    assert first.request.request_hash == second.request.request_hash
    assert first.source_manifest.manifest_hash == second.source_manifest.manifest_hash


def test_downside_features_bind_both_asset_and_qqq_availability(
    repository_root: Path,
) -> None:
    baseline_inputs = _inputs()
    qqq = baseline_inputs.series["QQQ"]
    delayed_qqq = replace(
        qqq,
        available_ats=tuple(
            value + timedelta(minutes=7) for value in qqq.available_ats
        ),
        payload_hashes=tuple(
            canonical_hash({"original": value, "revision": "qqq-delayed"})
            for value in qqq.payload_hashes
        ),
    )
    delayed_inputs = replace(
        baseline_inputs,
        series={**baseline_inputs.series, "QQQ": delayed_qqq},
    )
    kwargs = {
        "config_bundle": _config(repository_root),
        "artifact": _artifact(),
        "parent_decision": _parent(),
        "evaluation_anchor": _anchor(),
        "prior_state": None,
    }
    baseline = build_prospective_request_evidence(
        market_inputs=baseline_inputs,
        **kwargs,
    )
    delayed = build_prospective_request_evidence(
        market_inputs=delayed_inputs,
        **kwargs,
    )

    def downside_feature(
        evidence: ProspectiveRequestEvidenceV1,
    ) -> CandidateFeatureValueV1:
        gld = next(
            item for item in evidence.request.instruments if item.symbol == "GLD"
        )
        return next(
            item
            for item in gld.features
            if item.name == "downside_beta_126_qqq"
        )

    baseline_feature = downside_feature(baseline)
    delayed_feature = downside_feature(delayed)
    assert delayed_feature.available_at == max(delayed_qqq.available_ats[-127:])
    assert delayed_feature.value == pytest.approx(baseline_feature.value)
    assert delayed_feature.source_hash != baseline_feature.source_hash
    assert delayed.request.request_hash != baseline.request.request_hash
