# Recursive Improvement

> **Implementation status: Phase 0, PR 1, and PR 2**
>
> `recursive_improvement.enabled=false` is enforced by the versioned research
> configuration. The repository currently provides contracts, an immutable
> experiment-outcome ledger, deterministic memory materialization, a
> deterministic Meta Controller, and V2 Commander contracts. Automatic
> invocation remains disabled. It does not optimize a research portfolio, perform chronological
> meta-OOS evaluation, promote a Challenger automatically, or route a real
> order.

## Objective

The recursive-improvement design records what the research system tried, what
was known when it tried it, and what happened after a predeclared evaluation
horizon. A later controller may use that verified history to improve the mix of
research actions. The current implementation creates the audit substrate only.

This is not evidence that the system has found alpha, improved Sharpe, or
generalized out of sample. Predictions and realized metrics stored in the
ledger are observations under declared contracts, not performance claims.

## Delivery boundary

| Scope | Current state |
| --- | --- |
| Phase 0 safety/configuration boundary | Implemented |
| Candidate patch policy V2 and `AlgorithmProposalV2` schema | Implemented as contracts |
| PR 1 experiment action/outcome ledger | Implemented |
| PR 1 deterministic research-memory snapshot | Implemented |
| PR 1 manual CLI and typed scheduler work contracts | Implemented but disabled for automatic recursive maintenance |
| PR 2 deterministic Meta Controller | Implemented; manual/dry-run by default |
| PR 2 ResearchRequest/Decision V2 | Implemented alongside unchanged V1 |
| PR 3 portfolio delta-Sharpe judge | **UNIMPLEMENTED** |
| PR 4 chronological meta-OOS | **UNIMPLEMENTED** |

## Phase 0 contracts

### Disabled-by-construction activation

`config/research/research-plane.yaml` contains the versioned
`recursive_improvement` block. Its `enabled` field is a literal `false`, not a
runtime switch that an AI process can turn on. The same configuration keeps:

- `real_order_routing=false`;
- automatic promotion disabled;
- manual Champion approval and designation required;
- promotion OOS and meta-audit observations excluded from future training;
- the recursive Candidate patch policy bound to an exact version and hash.

Changing these safety contracts requires ordinary reviewed product development.
It is not a Candidate action.

### Candidate patch policy V2

New recursive candidates use `candidate_patch_policy_v2`. It allows only
new versioned Challenger implementation, Challenger configuration, candidate
tests, and Challenger research documentation under dedicated paths. Every V2
unified-diff section must be a new-file addition from `/dev/null`; modifying,
deleting, renaming, or copying any existing file is rejected even inside an
allowed Challenger namespace. It also rejects:

- research control-plane and persistence changes;
- risk, execution, ledger, security, or broker changes;
- migrations, workflows, and research configuration changes;
- Champion-owned paths;
- undeclared paths, path traversal, binary patches, and symbolic links;
- a patch with no candidate implementation or no candidate test.

The policy contract hash is sealed in configuration. The historical V1
inspector remains available unchanged for replaying artifacts produced under
the earlier contract.

### Proposal V2

`AlgorithmProposalV2` extends the immutable V1 proposal with:

- one typed primary research action and at most three distinct secondary
  actions;
- sorted mechanism tags and predicted failure codes;
- a predicted portfolio delta-Sharpe interval;
- a declared complexity delta;
- a binding to `candidate_patch_policy_v2`.

The schema is exposed by `research schema`. A V2 proposal can now be accepted
only through a `ResearchRequestV2` bound to a funded immutable action plan. It
still does not authorize promotion, capital, or trading by itself.

## PR 1 outcome-learning flow

```text
host-accepted proposal
    -> immutable ResearchExperimentActionV1
    -> zero or more append-only ExperimentOutcomeEventV1 records
    -> verified point-in-time event prefix
    -> ResearchMemorySnapshotV1
```

The action fixes the experiment identity, parent and candidate versions,
information role, decision time, maturity time, prediction, evaluation
contract, source hashes with point-in-time availability, and provenance.
Learning-forward economics are ineligible without a bound Candidate artifact.
Outcomes are later appended as
technical, economic, censored, invalidated, or correcting events.

Economic metrics cannot be populated before the action's maturity time.
Corrections append a new event that explicitly supersedes an earlier event;
they never rewrite the old row. Every event is bound to its canonical payload,
its maturation input, its sequence, and the previous event hash.

The memory snapshot is materialized from repository-verified event chains at an
explicit `as_of` and `data_available_cutoff`. It excludes future records,
pending or invalid records, promotion OOS, and meta-audit data. Only mature,
typed `LEARNING_FORWARD` economic events are eligible for future meta-training.

See [Experiment outcome ledger](experiment-outcome-ledger.md) for the exact
roles, maturity rules, hash chain, CLI, and current limitations.

## Information firewall

Every experiment has exactly one information role:

| Role | Purpose in PR 1 | Eligible for future meta-training |
| --- | --- | --- |
| `DISCOVERY` | Records exploratory or diagnostic history | No |
| `LEARNING_FORWARD` | Predeclared forward learning observation | Only after a typed, mature economic outcome |
| `PROMOTION_OOS` | Promotion lockbox evidence | No; excluded from memory |
| `META_AUDIT` | Future outer audit of the adaptive process | No; excluded from memory |

This separation prevents a promotion result or future outer-audit result from
quietly becoming training data for the controller it evaluates.

## Scheduler and operator surfaces

The scheduler domain defines an ordered maintenance chain:

```text
DAILY_AGGREGATION
    -> OUTCOME_MATURATION
    -> RESEARCH_MEMORY_MATERIALIZATION
```

Each successor may be leased only after its predecessor has an append-only
`SUCCEEDED` event for the same versioned market session, schedule, and config
hash. As with the existing Research scheduler, dispatch creates a typed receipt;
it does not call a model or execute the work itself.

Because `recursive_improvement.enabled=false`, normal schedule planning does not
create the two recursive-maintenance work items. The repository has no
automatic production consumer that calculates economic outcomes or invokes the
Meta Controller. Operators may build a deterministic action plan explicitly.

Operators and tests can inspect or exercise the PR 1 substrate manually:

```powershell
uv run python -m trading.cli research outcome mature --as-of <UTC_TIMESTAMP>
uv run python -m trading.cli research outcome mature `
  --input .local/research/outcome.json
uv run python -m trading.cli research memory materialize `
  --as-of <UTC_TIMESTAMP> `
  --data-available-cutoff <UTC_TIMESTAMP> `
  --created-at <UTC_TIMESTAMP>
uv run python -m trading.cli research meta-policy build `
  --snapshot-id <IMMUTABLE_SNAPSHOT_ID> `
  --research-cycle-id <NEW_CYCLE_ID> `
  --regime-cluster-id <REGIME> `
  --failure-cluster-id <FAILURE> `
  --portfolio-exposure-cluster-id <EXPOSURE> `
  --maximum-total-submissions 3 `
  --idempotency-key <UNIQUE_KEY>
uv run python -m trading.cli research status
uv run python -m trading.cli research schema
```

Mutation commands default to dry-run. `--commit` is required to append an
outcome, persist a memory snapshot, or persist an action plan. The outcome input must already have been
validated by a trusted host process; the CLI does not manufacture performance
measurements.

## V1 compatibility

PR 1 does not reinterpret historical records:

- `AlgorithmProposalV1` remains readable and hash-valid.
- A V1 proposal can be represented in the new action ledger only as
  `UNKNOWN_LEGACY`.
- Legacy actions carry no V2 prediction interval and are never eligible for
  meta-training, even when labeled `LEARNING_FORWARD`.
- Candidate patch policy V1 remains available for historical artifact replay.
- V2 is a separate contract for new recursive experiments; it does not mutate
  a stored V1 proposal or patch judgment.

## PR 2 deterministic research policy

The trusted `MetaControllerV1` builds a controller-only training view from the
exact event hashes in one immutable memory snapshot. It filters out protected
information roles, separates technical failures from economic rewards, applies
the versioned hierarchical contextual UCB formula, and emits one immutable
`ResearchActionPlanV1`. No random sampling is used.

`build_research_request_v2()` accepts snapshot and plan IDs rather than
caller-supplied performance/failure/regime summaries. It derives those
summaries from verified persistence and binds the V2 request and decision to
both hashes. Public and Commander repositories carry byte-identical schemas
and a common hash manifest.

See [Deterministic Meta Controller](meta-controller.md).

## Follow-up extension contracts

- [Meta-controller](meta-controller.md): implemented but disabled for automatic
  scheduling.
- [Portfolio delta Sharpe](portfolio-delta-sharpe.md): implemented trusted
  whole-portfolio paired evaluation, OOS V2, shadow V2, and Promotion V2;
  automatic operation remains disabled.
- [Chronological meta-OOS](chronological-meta-oos.md): **UNIMPLEMENTED**,
  planned outer chronological audit of the adaptive policy.

PR 4 remains a design contract until its stacked change lands. None of the
current recursive components claims real-world alpha.
