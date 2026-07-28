# Deterministic Meta Controller V1

> **Implemented, disabled by default.**
>
> The trusted host can build and persist deterministic research-action plans,
> but `recursive_improvement.enabled=false`. No scheduler automatically invokes
> the controller. It cannot create code, choose securities, allocate capital,
> promote a Challenger, or route an order.

## Responsibility

The Research Commander creates economic hypotheses and the Candidate Builder
implements an approved proposal. `MetaControllerV1` does one narrower job: it
ranks typed research actions and divides a pre-existing submission budget
using only an immutable point-in-time `ResearchMemorySnapshotV1`.

The controller is a pure, replayable hierarchical contextual UCB calculation.
It is not an LLM and does not receive raw OOS observations or raw daily return
series.

The context key is:

```text
(regime_cluster_id,
 failure_cluster_id,
 portfolio_exposure_cluster_id,
 primary_action_kind)
```

Sparse observations back off in this fixed order:

```text
(regime, failure, exposure, action)
(regime, failure, action)
(failure, action)
(regime, action)
(action)
global
```

## Trusted training view

`MetaControllerRepository.build_training_view()` resolves the exact event
hashes recorded in a persisted memory snapshot and verifies every event chain
again. It includes only records that:

- were available at or before the snapshot cutoff;
- have information role `LEARNING_FORWARD`;
- use a typed action other than `UNKNOWN_LEGACY`;
- contain either a technical result or an eligible matured economic result.

`PROMOTION_OOS`, `META_AUDIT`, future, unmatured, corrected-away, invalid, and
legacy records cannot become economic reward samples. A technical build or
test failure remains a technical observation; it is never converted into a
fabricated negative Sharpe.

## Reward and score

For each eligible matured economic observation:

```python
reward = clip(
    portfolio_delta_sharpe_lcb
    - turnover_penalty_weight
      * max(0, turnover_delta / turnover_scale)
    - drawdown_penalty_weight
      * max(0, drawdown_delta / drawdown_scale)
    - cost_penalty_weight
      * max(0, cost_delta_bps / cost_scale_bps)
    - complexity_penalty_weight
      * max(0, complexity_delta / complexity_scale),
    -reward_clip,
    reward_clip,
)
```

At the selected contextual bucket:

```python
shrunk_mean = (
    prior_strength * parent_or_global_mean
    + sum_matured_rewards
) / (prior_strength + matured_count)

exploration_bonus = max(
    exploration_floor,
    exploration_coefficient * sqrt(
        log(1 + total_matured_outcomes)
        / (prior_strength + matured_count)
    ),
)

technical_penalty = technical_failure_weight * (
    technical_failure_count / max(1, total_attempt_count)
)

score = shrunk_mean + exploration_bonus - technical_penalty
```

No random sampling is used. Rankings sort by descending score and then the
canonical `ResearchActionKind` value. Identical snapshot, context, config, and
budget inputs therefore produce the same plan hash.

The checked-in numbers are infrastructure defaults, not calibrated evidence of
profitability:

| Parameter | Value |
| --- | ---: |
| policy version | `hierarchical-contextual-ucb-v1` |
| maximum funded actions | 3 |
| prior strength | 4.0 |
| exploration coefficient / floor | 0.25 / 0.01 |
| technical failure weight | 0.25 |
| reward clip | 1.0 |
| turnover weight / scale | 0.05 / 1.0 |
| drawdown weight / scale | 0.10 / 0.05 |
| cost weight / scale | 0.05 / 10 bps |
| complexity weight / scale | 0.05 / 1.0 |

## Immutable action plan

`ResearchActionPlanV1` binds:

- the cycle, policy, config, memory snapshot, training view, and context hashes;
- every action score component, sample count, reason code, and allocated
  submission budget;
- maximum actions, total submissions, generation time, idempotency key, and
  canonical plan hash.

Migration `0015_meta_controller_v1` stores plans and accepted
`AlgorithmProposalV2` records in append-only tables with SQLite and PostgreSQL
UPDATE/DELETE guards. One cycle has at most one plan. Reusing an ID or
idempotency key with different bytes fails closed.

The plan may select only action kinds and submission counts. It has no fields
for symbols, formulas, parameters, position weights, OOS thresholds, Champion
designation, or broker actions.

## ResearchRequestV2

V1 request and decision replay remains unchanged. A new V2 request embeds the
exact immutable memory snapshot and action plan. The trusted host loads both by
ID from persistence and derives the performance, failure, and regime summaries;
there is no caller argument through which a forged summary can be injected.

An `AlgorithmProposalV2.primary_action_kind` must be one of the funded plan
actions. `NO_RESEARCH_CHANGE` and `REQUEST_MORE_EVIDENCE` remain valid, so a
plan never forces a candidate into existence.

Both Commander implementations use the same canonical schemas and hash
manifest under `contracts/research-v2/`. Snapshot text is observation data, not
an instruction. The isolated Builder receives only the approved structured
proposal, sanitized request binding, constraints, and clean source snapshot;
it does not receive the full Research Memory or Commander transcript.

## Operator command

The command defaults to dry-run:

```powershell
uv run python -m trading.cli research meta-policy build `
  --snapshot-id <IMMUTABLE_SNAPSHOT_ID> `
  --research-cycle-id <NEW_CYCLE_ID> `
  --regime-cluster-id <REGIME> `
  --failure-cluster-id <FAILURE> `
  --portfolio-exposure-cluster-id <EXPOSURE> `
  --maximum-total-submissions 3 `
  --idempotency-key <UNIQUE_KEY>
```

Add `--commit` to append the plan. `research status` reports the plan count and
continues to display `automatic_execution_enabled=false`,
`automatic_promotion_enabled=false`, and `real_order_routing=false`.

## Current limitation

The controller has not demonstrated useful research selection on real
chronological outer-audit data. PR2 validates determinism, PIT exclusion,
contextual backoff, exploration, and isolation only. Portfolio-level
delta-Sharpe evaluation and chronological meta-OOS are separate gates.
