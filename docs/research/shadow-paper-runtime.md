# Generic matched Research shadow runtime

`research_shadow_runtime_v1` is the paper-only execution path for one
Research Lifecycle Champion/Challenger pair. It does not route broker orders,
and both the runtime specification and every committed cycle persist
`real_order_routing=false`.

## Prospective target-state evidence is not shadow

`candidate_prospective_v1` is a pre-gate evidence producer. It evaluates a
sealed Candidate against the same parent decision cutoff and stores a
deterministic target response. It deliberately has no independent cash,
positions, orders, fills, ledger, NAV, or returns.

Only a Challenger that has passed every mandatory falsification check and the
locked OOS gate may enter the matched runtime described below. Prospective
observations cannot be relabeled as shadow performance and cannot advance the
Challenger lifecycle.

## Operator workflow

An OOS V2 `PASS` leaves the Challenger at `SHADOW_PENDING`. Starting the runtime
is a separate, explicit, dry-run-first operation:

```powershell
uv run python -m trading.cli research shadow-runtime activate `
  --plan .local/research/shadow/activation.json
uv run python -m trading.cli research shadow-runtime activate `
  --plan .local/research/shadow/activation.json `
  --commit
```

The activation plan binds the exact OOS result, registered shadow pair,
submission, predeclared Champion portfolio manifest, runtime code version,
expiry, and idempotency key. The commit writes the lifecycle start before
initializing the independent paper runtime. If initialization is interrupted,
repeating the identical plan repairs the missing runtime idempotently; a
different key or Champion binding is rejected.

A trusted target producer must create both artifact-bound target decisions and
one common quote bundle. The CLI never accepts returns, P&L, fills, or broker
instructions:

```powershell
uv run python -m trading.cli research shadow-runtime cycle `
  --input .local/research/shadow/matched-cycle.json
uv run python -m trading.cli research shadow-runtime cycle `
  --input .local/research/shadow/matched-cycle.json `
  --commit
```

The first command is a pure preview. Commit appends both arms in one database
transaction. The same decision time is an idempotency fence: an exact retry
returns the persisted cycle, while different targets or quotes fail closed.
Inspect and replay without writing:

```powershell
uv run python -m trading.cli research shadow-runtime status
uv run python -m trading.cli research shadow-runtime status --run-id <RUN_ID>
uv run python -m trading.cli research shadow-runtime replay --run-id <RUN_ID>
```

## Entry gate

The durable adapter starts only after the Research database contains:

- exactly one `CHAMPION` and one `CHALLENGER` registration for the same pair;
- a common `ShadowExecutionContract`;
- an OOS result binding the Challenger artifact hash; and
- the append-only `EXPLICIT_SHADOW_START` event written by
  `ResearchLifecycle.start_shadow`.

The Champion artifact hash is supplied explicitly at initialization and then
frozen into the runtime specification. Every target decision must reproduce
the exact arm, role, strategy ID, strategy version, artifact hash, market-input
manifest, quote manifest, and runtime-contract hash. A mismatch fails closed.

## Matched execution

Both arms have independent cash, positions, orders, fills, ledger entries, NAV,
and state snapshots. They share:

- starting capital;
- market-input and quote manifests;
- decision time and signal cutoff;
- schedule and execution-scenario versions;
- cost-model and liquidity-policy versions; and
- numerical commission, delay, participation, ADV, precision, and
  sensitivity parameters.

Targets are long-only weights summing to one including `USD_CASH`. Orders are
planned deterministically from midpoint NAV. Sells execute before buys. A
paper fill uses the executable bid or ask plus the configured adverse delay
penalty, and its quantity is capped by displayed-size participation, ADV
participation, the remaining order, current holdings for sells, and settled
paper cash for buys. Residual quantities receive explicit terminal expiry
events.

Every fill produces balanced security, cash, and commission postings. The
runtime records base execution cost plus the configured 5 bp and 10 bp
sensitivity costs. Cash and security quantities are checked after every cycle;
negative cash, short positions, or leverage are rejected.

## Persistence and replay

The adapter reuses the existing generic paper tables:

- `runs` and `shadow_arms`;
- `portfolio_decisions`, `order_intents`, `order_events`, and `fills`;
- `ledger_transactions` and `ledger_postings`;
- `nav_snapshots` and `arm_state_snapshots`; and
- `domain_events` for one immutable matched-cycle record.

Stable IDs, append-only rows, and the common decision timestamp make retries
idempotent. A retry with different targets or quotes is rejected. Replay starts
from both cash-only initial states and recomputes every stored cycle; any result
hash mismatch fails the replay.

Daily summaries contain matched return differences, actual exposures,
turnover, commissions, base execution cost, and cost sensitivities. The
aggregate summary is descriptive evidence for a later trusted promotion gate.
It sets `profitability_claimed=false` and makes no profitability or statistical
significance claim.

## Current schema constraint

Research registration permits an arm ID up to 100 characters, while the
existing generic paper tables store `arm_id` in 30 characters. To avoid a
silent truncation or a migration owned by another workstream, this runtime
rejects registered arm IDs longer than 30 characters. Normal generated arm IDs
must remain within that limit until a reviewed schema migration widens the
generic paper columns.

## Current settlement constraint

`research_shadow_runtime_v1` is conservative paper execution but not yet the Q1
settlement engine. It models sale proceeds as same-cycle paper cash and therefore
does not create T+1 unsettled receivables. Status exposes this without ambiguity:

```text
settlement_model=SAME_CYCLE_PAPER_CASH_V1
unsettled_receivables_supported=false
```

Do not treat its buying-power path as production-broker realism. Matched
Champion/Challenger attribution remains valid only when both arms use the same
runtime contract. A future settlement-aware runtime must use a new versioned
contract; it must not reinterpret or rewrite V1 cycles.
