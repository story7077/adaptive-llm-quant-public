# Algorithm proposal contract

## Purpose

`ResearchRequestV1`, `ResearchDecisionV1`, and `AlgorithmProposalV1` form the
common contract for both supported Research Commanders. The contract makes a
research proposal reproducible and falsifiable before any code is built.

Natural-language discussion is not an executable proposal. Only a schema-valid,
hash-bound decision may enter the Candidate Builder.

## `ResearchRequestV1`

The request includes:

| Group | Required content |
| --- | --- |
| Identity | `schema_version`, `request_id`, `research_cycle_id`, `selected_commander`, `commander_selection_id`, `commander_selection_version` |
| Time | `created_at`, `as_of`, `data_available_cutoff`, `expires_at` |
| Source binding | `source_snapshot_commit`, `champion_version`, `experiment_family`, `context_manifest_hash` |
| Current state | Champion manifest, active Challenger manifests, performance and failure summaries |
| Market context | Regime, execution-cost, capacity, market-evidence, and web-research summaries |
| Data authority | Versioned `available_data_catalog` |
| Authority | Allowed scope, forbidden scope, experiment budget |

The time order is validated, the expiry must follow creation, and the canonical
payload hash must equal `context_manifest_hash`.

The exact append-only Commander selection record is part of that hash. A request
created under one selection ID and version stays stale after any later selection
change, even if the operator eventually selects the same Commander kind again.

## `ResearchDecisionV1`

Both `CODEX_SOL_MAX` and `WEBGPT_SOL_PRO` return the same schema. Valid decisions
are:

```text
NO_RESEARCH_CHANGE
PROPOSE_NEW_STRATEGY
PROPOSE_STRATEGY_REVISION
PROPOSE_FEATURE_REVISION
PROPOSE_CALIBRATION_REVISION
RETIRE_STRATEGY
REQUEST_MORE_EVIDENCE
```

Proposal decisions require `proposal`. `REQUEST_MORE_EVIDENCE` requires a
non-empty `requested_evidence` list. Other decisions cannot contain those
payloads.

Before acceptance, the host verifies exact equality with the request for:

- request and cycle IDs;
- selected Commander and exact append-only selection ID/version;
- source snapshot commit;
- Champion version;
- experiment family;
- context hash and request schema;
- request expiry.

The current append-only Commander selection must also still match. Outputs
received at or after expiry are invalid.

## `AlgorithmProposalV1`

### Hypothesis and version

| Field | Meaning |
| --- | --- |
| `proposal_id` | Immutable proposal identity |
| `hypothesis_id` | Experiment hypothesis identity/version |
| `hypothesis` | Testable claim |
| `economic_mechanism` | Why the edge should exist |
| `why_current_model_failed` | Evidence-based diagnosis |
| `parent_strategy_id` / `parent_strategy_version` | Immutable parent |
| `proposed_strategy_id` / `proposed_strategy_version` | New Challenger identity |

The proposed version may not overwrite the parent version. A parameter change
after observing OOS feedback is a new hypothesis version and submission.

### Scope and data

| Field | Meaning |
| --- | --- |
| `target_horizon` | Decision/holding horizon |
| `target_universe` | Explicit symbols from the bound data catalog |
| `required_data` | Required PIT datasets |
| `files_allowed_to_change` | Patch authority for this proposal |
| `tests_required` | Proposal-specific tests in addition to mandatory gates |
| `evidence_source_ids` | Evidence in the current bound bundle |

The target universe is catalog-driven and may contain eligible US-listed
equities and ETFs. It is not restricted to a particular sector or leveraged
product. A target fails validation when it is absent from the catalog, lacks
mandatory daily history, lacks point-in-time membership data where required, or
cannot be executed in the matched shadow environment.

Evidence IDs outside the current request are rejected.

### Algorithm change

The proposal separately declares:

- `feature_changes`
- `signal_formula_changes`
- `entry_rule_changes`
- `exit_rule_changes`
- `position_sizing_changes`
- `regime_activation_changes`
- `calibration_changes`

This separation lets reviewers distinguish a new economic idea from a
calibration adjustment or implementation bug fix. The Candidate Builder may
implement only the declared scope.

### Edge and falsification

The proposal must state:

- `expected_edge_source`
- `expected_failure_modes`
- `invalidation_conditions`
- `placebo_tests`
- `stress_tests`
- `minimum_economic_effect`
- `estimated_capacity`
- `estimated_turnover`
- `estimated_cost_sensitivity`

The goal is a claim that can fail. Vague requests such as “improve performance”
or “find a better model” are insufficient.

`raw_confidence` is preserved for calibration research only. It cannot select
capital, bypass a test, alter OOS budget, or affect promotion.

## Versioning guidance

Use semantic intent:

- patch version: implementation correction without changing the economic
  hypothesis;
- minor version: compatible feature, calibration, or rule revision;
- major version: new economic mechanism, target horizon, or materially changed
  behavior.

Examples:

```text
T1 1.0.0  Champion
T1 1.1.0  Challenger: point-in-time breadth revision
T1 1.1.1  Challenger: deterministic bug fix
T1 2.0.0  Challenger: different economic hypothesis
```

Every implementation produces `ChallengerManifestV1`, binding:

- proposal and hypothesis;
- parent and candidate versions;
- source commit;
- patch, proposal, code, config, and test-manifest hashes;
- Commander and Builder identities;
- evidence IDs, required data, horizon, execution universe, turnover, and
  capacity;
- initial status and creation time.

## Minimal illustrative shape

This example is intentionally incomplete and contains no trading recommendation:

```json
{
  "decision": "PROPOSE_FEATURE_REVISION",
  "proposal": {
    "hypothesis": "A point-in-time breadth feature may reduce concentration-dependent trend failures.",
    "economic_mechanism": "Cross-sectional participation can distinguish broad persistence from narrow index leadership.",
    "parent_strategy_id": "T1",
    "parent_strategy_version": "1.0.0",
    "proposed_strategy_id": "T1",
    "proposed_strategy_version": "1.1.0",
    "target_universe": ["AAA", "BBB", "CCC"],
    "invalidation_conditions": [
      "Net matched OOS effect is below the predeclared threshold",
      "The effect disappears after sector and momentum neutralization"
    ]
  }
}
```

Production schema fields, hashes, times, data catalog, evidence references, and
tests are still mandatory; use `trading.cli research schema` to obtain the
authoritative JSON schemas.

## Rejection conditions

The host rejects a proposal when:

- its hash or request binding is wrong;
- it is stale or expired;
- its Commander no longer matches the selected version;
- it cites unavailable evidence;
- its universe is outside the bound catalog;
- required PIT or shadow data is unavailable;
- it attempts a protected path or Champion mutation;
- it omits falsification, failure-mode, cost, capacity, or economic-effect
  declarations.

Rejection creates no code change, shadow arm, or operational effect.
