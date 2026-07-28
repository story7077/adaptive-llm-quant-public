# Chronological Meta-OOS Extension Contract

> **UNIMPLEMENTED — planned follow-up PR 4**
>
> The checked-in configuration fixes `meta_oos.enabled=false`. There is no
> outer-audit reservation, chronological meta-policy evaluator, seed audit,
> result table, scheduler worker, CLI, or promotion gate for meta-OOS.

## Purpose

Promotion OOS asks whether a specific Challenger survived a locked evaluation.
Meta-OOS asks a different question:

> Did the adaptive research policy improve future research outcomes when its
> choices were made using only information available at each historical time?

The outer audit evaluates the policy-selection process, not an individual
strategy. Its observations must never be fed back into the policy instance
being audited.

## Information partitions

The existing ledger reserves four roles:

| Role | Future meta-policy use |
| --- | --- |
| `DISCOVERY` | Context or diagnostic history only, subject to the future contract |
| `LEARNING_FORWARD` | Eligible training history after maturity |
| `PROMOTION_OOS` | Individual-Challenger promotion evidence; excluded from training |
| `META_AUDIT` | Outer chronological audit; excluded from training |

PR 1 already excludes `PROMOTION_OOS` and `META_AUDIT` events from research
memory snapshots. PR 4 must preserve that firewall in the evaluation service
and its persistence schema.

## Required chronological protocol

A future meta-OOS evaluation must:

1. predeclare ordered train, decision, maturity, and outer-audit windows;
2. freeze policy code, configuration, seed, and memory at each decision cutoff;
3. allow training only on events available and mature by that cutoff;
4. generate or replay the policy decision without seeing its later outcome;
5. score the decision only after the predeclared outcome horizon matures;
6. advance chronologically without revising prior decisions;
7. aggregate outer-audit results without returning protected row-level data to
   the Commander, Builder, or controller.

Appending a later event must not alter any earlier decision, policy state, or
audit hash.

## Reserved safety flags

The Phase 0 configuration reserves:

```yaml
meta_oos:
  enabled: false
  require_outer_audit_reservation: true
  prohibit_best_seed_selection: true
```

These values are validated configuration, not a working service.

An outer-audit reservation must be consumed append-only and exactly once. The
policy, experiment family, chronological windows, source hashes, and seed must
be fixed before protected results are opened. Choosing the best seed,
hyperparameter set, or policy variant after inspecting the outer audit is
forbidden; any change starts a new hypothesis/version and consumes a new
predeclared budget.

## Required future result boundary

The outer service should return only a bounded verdict, predeclared aggregate
statistics, reason codes, common decision count, budget usage, and artifact
hashes. It must not expose:

- protected dates or per-experiment returns;
- candidate selection details that reveal the lockbox sequence;
- raw market or outcome observations;
- a seed leaderboard;
- data that can be used to tune the same policy against the audit window.

Any audit result stored in the experiment ledger must use
`information_role=META_AUDIT` and remain ineligible for meta-training.

## Acceptance conditions for PR 4

PR 4 is not complete until tests demonstrate:

- strict chronological train/decision/maturity/audit separation;
- no future-data effect on past policy decisions;
- exact one-time reservation and experiment-budget accounting;
- deterministic replay for a fixed predeclared seed;
- rejection of best-seed and post-audit parameter selection;
- exclusion of all meta-audit observations from later memory/training;
- bounded aggregate-only output;
- append-only persistence and hash verification;
- no automatic promotion, Champion mutation, broker access, or real routing.

## Interpretation

A passing outer audit would be evidence about one frozen adaptive-policy
version under one predeclared protocol. It would not guarantee stable alpha,
future profitability, or live execution quality. The current repository has
not run such an audit and makes no meta-OOS performance claim.
