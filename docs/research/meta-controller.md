# Meta-Controller Extension Contract

> **UNIMPLEMENTED — planned follow-up PR 2**
>
> This document is a design boundary, not production behavior. There is no
> meta-controller module, trained policy, scheduler consumer, Commander
> integration, or automatic research-action selection in the current
> repository. `recursive_improvement.enabled=false`.

## Intended responsibility

A future meta-controller may rank or select a small set of research action
types from a point-in-time `ResearchMemorySnapshotV1` and the current
hash-bound research request. Its output must remain a research recommendation:
it may lead to a new versioned proposal, but it may not edit the Champion,
change trusted judges, promote a Challenger, allocate live capital, or create a
broker order.

The controller is separate from:

- the Web Scout, which gathers evidence;
- the Research Commander, which writes the economic hypothesis and proposal;
- the Candidate Builder, which implements an approved proposal;
- falsification, OOS, shadow, and promotion judges;
- the Operational Trading Plane.

## Reserved configuration

The Phase 0 configuration reserves, but does not execute, this policy contract:

| Field | Checked-in value |
| --- | ---: |
| `policy_version` | `hierarchical-contextual-ucb-v1` |
| `maximum_actions_per_cycle` | 3 |
| `prior_strength` | 4.0 |
| `exploration_coefficient` | 0.25 |
| `technical_failure_weight` | 0.25 |
| `reward_clip` | 1.0 |
| turnover penalty weight / scale | 0.05 / 1.0 |
| drawdown penalty weight / scale | 0.10 / 0.05 |
| cost penalty weight / scale | 0.05 / 10 bps |
| complexity penalty weight / scale | 0.05 / 1.0 |

These values are configuration placeholders bound into the research manifest.
Their presence does not mean that a contextual-bandit score, reward update, or
policy state currently exists.

## Required future input contract

PR 2 must bind every decision to at least:

- policy and schema versions;
- exact `ResearchMemorySnapshotV1` ID and hash;
- request, cycle, config, source, Champion, and available-data-catalog hashes;
- `as_of` and `data_available_cutoff`;
- experiment-family submission and OOS budgets;
- available typed action kinds and any action-level constraints;
- a deterministic seed if randomized exploration is used.

Only records permitted by the information firewall may influence policy state.
`PROMOTION_OOS`, `META_AUDIT`, unmatured economics, corrected-away records, and
legacy `UNKNOWN_LEGACY` observations must not become reward samples.

## Required future output contract

The result must be an immutable, hash-bound recommendation containing:

- selected typed action kinds, never more than the configured maximum;
- the context and memory hashes used;
- policy version and deterministic seed;
- score components and uncertainty sufficient for replay;
- explicit no-action and insufficient-evidence outcomes;
- an expiry and idempotency key.

The output must not contain order quantities, target weights, broker actions,
promotion decisions, or changes to protected code. A Research Commander must
still produce the economic rationale and a valid `AlgorithmProposalV2`.

## Acceptance conditions for PR 2

PR 2 is not complete until tests demonstrate:

- identical versioned inputs produce an identical decision and policy update;
- later events cannot change a past decision;
- protected information roles never enter training or reward;
- technical failures and economic rewards remain distinct;
- censored and corrected events are handled without rewriting history;
- exploration is bounded by the configured action count and experiment budget;
- missing, invalid, or insufficient memory yields no action;
- policy state is append-only, replayable, and independently hash-verified;
- the controller cannot touch risk, execution, ledger, broker, promotion, or
  real-routing paths.

## Current non-capabilities

No score, posterior, hierarchy, contextual feature vector, penalty formula,
online update, or production policy state is implemented. The action and memory
statistics in PR 1 are data contracts only. They must not be described as an
adaptive controller until a later PR implements and validates this contract.
