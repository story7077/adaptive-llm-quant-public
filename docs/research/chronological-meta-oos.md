# Chronological Meta-OOS V1

> **Implemented, disabled for automatic operation**
>
> The trusted host can predeclare, reserve, run, persist, and verify a
> chronological outer audit. `recursive_improvement.enabled=false` and
> `meta_oos.enabled=false` keep automatic scheduling disabled. The CLI does not
> accept raw audit returns, automatic promotion remains unavailable, and
> `real_order_routing=false` remains enforced.

## Question being tested

Promotion OOS asks whether one Challenger survived a locked evaluation.
Chronological meta-OOS asks whether one frozen adaptive research policy made
better future research choices than simpler policies when every choice used
only information available at that historical time.

The mandatory matched arms are:

| Arm | Policy |
| --- | --- |
| `STATIC_CHAMPION` | Keep the initial Champion unchanged |
| `FIXED_RECALIBRATION` | Apply only a predeclared schedule and rule; never learn from research outcomes |
| `MEMORYLESS_COMMANDER` | See current epoch context, but no prior outcome ledger or Meta Controller state |
| `ADAPTIVE_META_CONTROLLER` | Use only prior, mature `LEARNING_FORWARD` outcomes and a versioned adaptive policy |

Each arm has an independent policy state, experiment ledger, budget, selected
strategy state, and return sequence. Arms share only the predeclared matched
market, cost, execution, and epoch contracts.

## Immutable plan

`ChronologicalMetaOosPlanV1` binds:

- plan ID/version and initial Champion manifest hash;
- the exact four arms and each adapter version;
- Commander model/schema bindings and Meta Controller version;
- ordered epoch definitions;
- cost model, execution model, bootstrap, controller configuration, source
  snapshot, and outer dataset hashes;
- candidate-generation and OOS budgets;
- outer-audit dataset ID and budget ordinal;
- creation time, immutable plan hash, and `real_order_routing=false`.

Every epoch predeclares discovery start/end, decision time, purge horizon,
embargo sessions, forward start/end, outcome availability, market-data
manifest, and budgets. Validation rejects overlaps, reversals, insufficient
purge/embargo, an invalid decision-to-forward boundary, or any epoch order that
would allow a later observation to alter an earlier choice.

The checked-in evaluation contract requires 8–52 epochs. These are initial
contract values, not calibrated evidence that the policy is useful.

## Point-in-time execution

For each epoch and arm the trusted runner:

1. materializes context containing only records available by `decision_at`;
2. materializes an arm-private memory view;
3. asks that arm's version-bound `ResearchPolicyAdapter` for an action or
   no-change;
4. enforces the same candidate-generation and OOS budgets;
5. validates every proposal, Candidate artifact, input, and invocation binding;
6. runs the selected strategy once over the predeclared forward window;
7. makes the resulting learning outcome eligible only at
   `outcome_available_at`;
8. advances to the next epoch without revising prior decisions.

Candidate reuse requires:

```text
candidate.first_available_at <= epoch.decision_at
proposal.created_at <= epoch.decision_at
every source.available_at <= epoch.decision_at
```

The runner rejects future candidates, backdating, current-forward feedback,
future or unmatured learning outcomes, `PROMOTION_OOS` or `META_AUDIT` learning
inputs, cross-arm ledger/state reuse, plan/hash mismatches, and a changed
adapter version.

`MEMORYLESS_COMMANDER` always receives no research memory. The adaptive arm may
receive only earlier, mature `LEARNING_FORWARD` outcomes from its own arm.
Appending a later record therefore cannot change a prior decision hash.

## Policy adapters and model boundary

The mathematical runner depends on `ResearchPolicyAdapter`, not an LLM client:

```python
class ResearchPolicyAdapter(Protocol):
    def plan_research(
        self,
        *,
        epoch_context,
        research_memory_snapshot,
        budget,
    ) -> MetaOosPolicyDecisionV1: ...
```

Implemented adapters are:

- `StaticChampionPolicyAdapter`;
- `FixedRecalibrationPolicyAdapter`;
- `RecordedMemorylessCommanderAdapter`;
- `RecordedAdaptiveCommanderAdapter`;
- `SyntheticPolicyAdapter`.

Recorded Commander decisions bind the model, model version, prompt hash,
request hash, schema version, output hash, and invocation time. CI and unit
tests use deterministic recorded or synthetic adapters and never invoke
WebGPT, Codex, a network service, or a broker.

## Outer-audit firewall

Migration `0017_chronological_meta_oos_v1` adds an isolated, append-only
namespace:

- `chronological_meta_oos_plans`;
- `meta_oos_outer_audit_reservations`;
- `meta_oos_epoch_arm_audit_records`;
- `chronological_meta_oos_results`.

SQLite and PostgreSQL block UPDATE and DELETE. PostgreSQL reservation uses a
dataset-level advisory lock; idempotency keys, plan hashes, dataset budget
ordinals, and database transactions prevent double consumption or a
changed-request retry.

The plan and thresholds are immutable before the protected dataset is opened.
The checked-in configuration permits one use per outer dataset and forbids
best-seed selection. A changed seed, policy, threshold, epoch, dataset, code,
or binding is a new plan and consumes a new predeclared budget.

Raw protected returns, dates, session keys, Candidate sequence details, and
bootstrap draws remain inside the trusted environment. They are neither stored
in the production experiment-outcome ledger nor returned to the Commander,
Builder, CLI, UI, database result row, or public artifact. Only one bounded,
aggregate result crosses the boundary.

## Aggregate result

`ChronologicalMetaOosResultV1` reports, per arm:

- net sequence return, annualized volatility, portfolio Sharpe, maximum
  drawdown, and configured-quantile tail loss;
- annualized turnover;
- experiment, Candidate, OOS-use, positive-matured-outcome, technical-failure,
  and promotion-eligible counts;
- regime and action aggregates;
- prediction calibration;
- experiments and OOS uses per positive DeltaSharpe lower bound.

Matched comparisons use the same private session sequence and the same paired
stationary-bootstrap indices. For every ordered non-self arm pair:

```text
DeltaSharpe
= Sharpe(candidate arm full sequence)
- Sharpe(baseline arm full sequence)
```

The three required adaptive comparisons are against `STATIC_CHAMPION`,
`FIXED_RECALIBRATION`, and `MEMORYLESS_COMMANDER`. The pass decision is
configuration-driven:

```text
all three adaptive DeltaSharpe LCBs > configured threshold
adaptive research efficiency >= configured minimum
adaptive maximum drawdown <= configured maximum
no PIT, chronology, budget, or binding violation
```

The sample Sharpe and paired stationary bootstrap reuse the V2
whole-portfolio estimator. Result, audit-record, decision, invocation, memory,
and plan hashes are deterministic for identical versioned inputs.

## CLI and trusted service

Validate a plan without writing:

```powershell
uv run python -m trading.cli research meta-oos plan `
  --input .local/research/meta-oos/plan.json
```

Persist the immutable plan only after review:

```powershell
uv run python -m trading.cli research meta-oos plan `
  --input .local/research/meta-oos/plan.json `
  --commit
```

Inspect readiness without consuming audit budget:

```powershell
uv run python -m trading.cli research meta-oos run `
  --plan-id <PLAN_ID>
```

Reserve the protected dataset for the trusted service:

```powershell
uv run python -m trading.cli research meta-oos run `
  --plan-id <PLAN_ID> `
  --idempotency-key <UNIQUE_KEY> `
  --commit
```

The `run` CLI cannot ingest returns. A trusted private service calls
`run_chronological_meta_oos`, then atomically persists the bounded epoch audit
hashes and final aggregate through `MetaOosRepository`.

Verify a persisted result, or a local aggregate file, read-only:

```powershell
uv run python -m trading.cli research meta-oos verify `
  --plan-id <PLAN_ID>
```

`research status` reports the plan, reservation, result, budget, and safety
state without protected rows.

## Failure and recovery

- Any future-data, chronology, availability, policy-binding, budget, arm
  isolation, hash, or non-finite metric violation fails closed.
- An interrupted run may retry only with the identical plan and idempotency
  identity. Immutable persistence makes the retry idempotent.
- An expired reservation must not be repurposed with changed inputs.
- A failed or non-passing result remains append-only and cannot be deleted.
- Meta-OOS does not mutate the Champion. A passing result is research evidence,
  not a promotion command.
- Automatic promotion and real broker routing remain unavailable.

## Interpretation

The deterministic synthetic fixtures verify chronology, isolation,
learning/no-learning relationships, and replay. They are not performance
evidence. Even a protected real-data pass would describe one frozen policy,
plan, dataset, and set of thresholds. It would not prove stable alpha,
statistical significance, future profitability, capacity, or live execution
quality.
