# Experiment Outcome Ledger

> **Implementation status: PR 1 audit substrate**
>
> The typed contracts, append-only persistence, migration, deterministic memory
> materialization, dry-run-first CLI, and scheduler work types are implemented.
> Automatic outcome measurement, automatic action registration, memory
> injection into Commander requests, and a production meta-controller are not
> implemented. `recursive_improvement.enabled=false`.

## Purpose

The ledger preserves the chronological relationship between a research action
and its later outcome. It prevents three common forms of contamination:

1. treating a technical build result as economic evidence;
2. training on a result before its declared forward horizon has elapsed;
3. feeding promotion OOS or outer-audit observations back into the policy being
   judged.

The ledger records evidence. It does not decide which research action to take
and does not prove that an action, strategy, or AI system is profitable.

## Contracts

### `ResearchExperimentActionV1`

One immutable action is registered per `experiment_id`. Its hash-bound payload
includes:

- cycle, proposal, Challenger, parent strategy, and candidate version;
- primary and secondary action kinds and mechanism tags;
- information role;
- `decision_at`, `maturity_due_at`, and `created_at`;
- optional predicted delta-Sharpe interval and predicted failures;
- complexity delta;
- Candidate artifact and evaluation-contract hashes;
- source-artifact hashes paired with source availability times that cannot
  exceed `decision_at`;
- legacy and meta-training permission flags;
- idempotency key and canonical `action_hash`.

The action must exist before an outcome can be prepared or appended.
Registration is exposed through the trusted repository API in PR 1; there is
no general action-registration CLI or automatic Commander-to-ledger pipeline.

### Action kinds

The V2 proposal contract supports:

```text
ADD_FEATURE
REMOVE_FEATURE
CHANGE_SIGNAL_FORM
ADD_REGIME_GATE
CHANGE_POSITION_SIZING
CHANGE_EXIT_RULE
ADD_DIVERSIFYING_SLEEVE
RECALIBRATE_PARAMETER
RETIRE_REDUNDANT_SLEEVE
REQUEST_NEW_DATA
```

`UNKNOWN_LEGACY` is reserved for V1 compatibility and cannot be selected by an
`AlgorithmProposalV2`.

### `ExperimentOutcomeEventV1`

Outcome events carry the action provenance plus:

- event kind and experiment stage;
- per-experiment sequence and previous-event hash;
- evaluation window, availability time, and maturity status;
- technical success/failure and structured failure codes;
- optional realized portfolio delta-Sharpe bounds and worst-cost lower bound;
- optional drawdown, tail-loss, turnover, and cost deltas;
- prediction error calculated from the stored median prediction;
- source hashes and point-in-time source availability;
- supersession, idempotency, maturation-input, and canonical event hashes.

Event kinds are:

```text
EXPERIMENT_REGISTERED
TECHNICAL_OUTCOME_RECORDED
ECONOMIC_OUTCOME_MATURED
OUTCOME_CENSORED
OUTCOME_INVALIDATED
OUTCOME_CORRECTED
```

Stages range from proposal/build/test/replay/falsification through OOS, shadow,
promotion, forward, and duplicate detection. They describe provenance; they do
not bypass any existing Challenger gate.

## Information roles

| Role | Included in a memory snapshot | Economic meta-training eligibility |
| --- | --- | --- |
| `DISCOVERY` | Yes when otherwise valid; useful for technical/failure history | Never |
| `LEARNING_FORWARD` | Yes when otherwise valid | Only a typed V2, `MATURED` event with economic values |
| `PROMOTION_OOS` | No | Never |
| `META_AUDIT` | No | Never |

An action's `meta_training_permitted` value is derived from role, typed V2
provenance, and the presence of a Candidate artifact hash.
An event's `eligible_for_meta_training` is recalculated from role, maturity,
typed action kind, Candidate artifact binding, and the presence of economic
measurements. Callers cannot turn either flag on independently.

## Maturation rules

Maturity status is one of:

```text
PENDING
MATURED
CENSORED
INVALIDATED
SUPERSEDED
```

The rules are fail-closed:

- `maturity_due_at` cannot precede `decision_at`;
- an event cannot be created before its `available_at`;
- economic values must remain `None` unless status is `MATURED`;
- economic values require both evaluation-window bounds;
- the evaluation window must be ordered and end no later than
  `available_at`;
- economic values cannot be available before `maturity_due_at`;
- each source artifact requires a corresponding source-availability time;
- source hashes and availability times are normalized and hashed as pairs;
- every declared source time must be no later than the outcome's
  `available_at`;
- outcome availability cannot precede its action and cannot regress within an
  experiment event chain;
- a technical failure requires at least one structured failure code;
- a technical success cannot carry a failure code;
- the maturation evaluation-contract hash must match the action.

A technical build/test failure may be recorded immediately without economic
metrics. It remains useful failure evidence but is not an economic
meta-training observation.

`due_experiments(as_of)` returns actions whose maturity time has arrived and
that have no event or whose latest event remains `PENDING`. It does not
calculate the missing outcome.

## Append-only hash chain

Each experiment has its own ordered event chain.

```text
event 1: previous_event_hash = null
event 2: previous_event_hash = event 1 event_hash
event 3: previous_event_hash = event 2 event_hash
```

The repository verifies on read:

- a contiguous sequence beginning at one;
- exact previous-hash linkage;
- non-regressing creation times;
- canonical payload, input, and row bindings;
- valid same-experiment supersession references.

An idempotency key may replay only the identical maturation input. Reusing it
with changed content fails. A correction appends a new event with
`supersedes_event_id`; the old event remains immutable and a given event cannot
be superseded twice. `EXPERIMENT_REGISTERED` may appear only at the beginning
of a chain. Once an effective economic, censored, invalidated, superseded, or
failed-technical terminal exists, only an `OUTCOME_CORRECTED` event that names
that terminal may follow.

PostgreSQL uses a per-experiment transaction advisory lock while appending.
Database uniqueness protects action identity, event sequence, idempotency, and
hashes. SQLite and PostgreSQL migration triggers reject `UPDATE` and `DELETE`
on all three ledger tables, and the ORM append-only guard provides the same
application boundary.

## Persistence

Migration `0014_experiment_outcome_ledger` adds:

| Table | Meaning |
| --- | --- |
| `research_experiment_actions` | One immutable action per experiment |
| `research_experiment_outcome_events` | Per-experiment append-only event chain |
| `research_memory_snapshots` | Immutable point-in-time aggregate snapshots |

The migration follows `0013_candidate_artifact_registry`. Downgrade removes
only the three PR 1 tables and their guards; it is destructive to rows in
those tables and is therefore a disposable-database validation operation, not
an operational rollback procedure. Historical migrations and older research
records keep their original meaning.

## Research memory snapshot

`ResearchMemorySnapshotV1` is built in one persistence transaction from event
rows that the trusted repository has revalidated. The caller supplies:

- `as_of`;
- `data_available_cutoff`, which cannot exceed `as_of`;
- `created_at`, which cannot precede `as_of`.

Only the event prefix with `created_at <= as_of` is considered. The materializer
then excludes:

- records with `available_at` after the data cutoff;
- `PROMOTION_OOS` and `META_AUDIT` roles;
- `PENDING`, `CENSORED`, `INVALIDATED`, or `SUPERSEDED` records;
- events superseded by a correction that is both in the event prefix and
  available by the snapshot data cutoff.

The result stores included event hashes, exclusion counts, per-action
statistics, failure-code clusters, reverse-chronological eligible historical
analogs, and prediction-error calibration. Economic statistics, analogs, and
calibration use only `eligible_for_meta_training` events. Regime-action
statistics are deliberately empty in PR 1 because the typed regime contract is
deferred.

The snapshot ID and hash bind its times, included hashes, aggregates, and
creation time. Re-materializing the same verified prefix with the same inputs is
idempotent; later appends do not change a past snapshot.

PR 1 does not yet impose an explicit top-k or byte budget on the snapshot and
does not inject it into `ResearchRequestV1`. A later prompt-facing integration
must define and test those bounds before treating the snapshot as model context.

## CLI

List due actions without writing:

```powershell
uv run python -m trading.cli research outcome mature `
  --as-of 2026-07-28T00:00:00Z
```

Validate one host-produced maturation file without writing:

```powershell
uv run python -m trading.cli research outcome mature `
  --input .local/research/outcome.json
```

Append it explicitly:

```powershell
uv run python -m trading.cli research outcome mature `
  --input .local/research/outcome.json `
  --commit
```

Materialize a memory snapshot in dry-run mode:

```powershell
uv run python -m trading.cli research memory materialize `
  --as-of 2026-07-28T00:00:00Z `
  --data-available-cutoff 2026-07-28T00:00:00Z `
  --created-at 2026-07-28T00:00:00Z
```

Add `--commit` to persist it. Both mutation commands default to dry-run and
report `real_order_routing=false`.

`research schema` exposes `AlgorithmProposalV2`,
`ResearchExperimentActionV1`, `ExperimentOutcomeMaturationInputV1`,
`ExperimentOutcomeEventV1`, and `ResearchMemorySnapshotV1`. `research status`
reports action, physical/effective event, eligible-learning-event, and snapshot
counts plus the latest snapshot.

## Scheduler integration

PR 1 adds typed dispatch targets for outcome maturation and memory
materialization. When explicitly included by a future enabled configuration,
the work ordering for a market session is:

1. daily aggregation succeeds;
2. outcome maturation may be leased;
3. outcome maturation succeeds;
4. memory materialization may be leased.

The scheduler writes plans, leases, receipts, and outcomes. It does not perform
the maturation calculation or memory job. With the checked-in configuration,
`recursive_improvement.enabled=false`, so these maintenance plans are not
created by the runtime service.

## V1 compatibility

`AlgorithmProposalV1` and Candidate patch policy V1 remain available for
historical replay. When a V1 proposal is registered in this ledger:

- its primary action becomes `UNKNOWN_LEGACY`;
- V2-only prediction and mechanism fields remain absent;
- `legacy_proposal=true`;
- meta-training permission is always false.

This avoids inventing labels or predictions for old experiments and prevents a
schema upgrade from silently turning historical outcomes into training data.

## Current limitations

- No production path automatically creates `ResearchExperimentActionV1` from a
  Commander decision.
- No trusted worker computes a maturation input from shadow or forward results.
- Candidate, proposal, and evaluation hashes are structurally recorded, but
  PR 1 does not yet verify a maturation input against a trusted PR 3 evaluator
  receipt. Committed CLI input is operator-supplied audit data, not promotion
  evidence.
- No scheduler consumer executes the two new dispatch targets.
- The legacy outcome CLI still accepts operator-supplied trusted-host audit
  data; it is not itself a DeltaSharpe or promotion evaluator.
- No typed regime descriptor is implemented.
- Automatic Meta Controller, portfolio DeltaSharpe, and meta-OOS scheduling
  remain disabled even though their trusted components are implemented.
- Nothing here can promote a Champion or route an order.
