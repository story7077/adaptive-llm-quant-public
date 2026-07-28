# Operations

## Operating modes

Adaptive LLM Quant has two independent operating planes:

- **Operational Trading Plane**: market ingestion, paper strategy cycles,
  deterministic risk, conservative fills, orders, ledger, NAV, and replay.
- **Research Plane**: evidence gathering, proposals, isolated candidate builds,
  falsification, OOS, shadow evaluation, and promotion eligibility.

Research failure must not delay or alter operational paper cycles. Neither plane
has real broker routing.

## Local prerequisites

Install:

- Python 3.12 or newer;
- `uv`;
- SQLite for isolated development or PostgreSQL for the persistence contract;
- headed Chrome, local CDP, and AGBrowse for Web Scout runs;
- a separate checkout of
  `adaptive-llm-quant-research-commander` for Codex Commander/Builder runs.

External browser/model tooling and credentials are user-managed and remain
outside this repository. Configure executable and artifact locations through
local environment variables described in
[WebGPT and AGBrowse research](webgpt-agbrowse-research.md). Do not commit
environment-specific paths.

## Q1 startup and point-in-time scheduling

Calendar synchronization persists historical sessions for signal and settlement
lookups, but it creates executable Q1 cycle slots only for sessions that have
not opened yet. A new Q1 run first started intraday therefore remains
`PENDING_BOOTSTRAP` until the next observed market session.

Do not manufacture or manually backfill the missed 09:30 bootstrap. A calendar
record first observed after a cycle's scheduled time cannot satisfy
`available_at <= scheduled_at`. Waiting for the next session preserves the PIT
contract and prevents a retryable historical bootstrap from starving later
cycles.

## First local validation

Run from the repository root:

```powershell
uv sync
uv run python -m trading.cli config validate --all
uv run python -m trading.cli db upgrade
uv run python -m trading.cli doctor
uv run pytest
uv run ruff check .
uv run pyright
```

`doctor` must show broker production gates disabled. A configuration that enables
real routing or automatic promotion is invalid.

Recursive improvement is also disabled in the checked-in contract:
`recursive_improvement.enabled=false`. The current branch implements Phase 0
through PR 4: the experiment-outcome ledger, immutable memory, deterministic
Meta Controller, V2 Commander contracts, portfolio DeltaSharpe/OOS V2, matched
shadow and Promotion V2, and chronological meta-OOS. None is automatically
invoked. Automatic promotion and real order routing remain unavailable.

### Isolated Candidate runtime connection

After a finalized Candidate artifact has been registered, use
`research candidate-execute` with one host-produced
`CandidateDecisionRequestV1` to verify the actual separate-repository runtime.
The Commander root and finalized run are explicit local arguments and must never
be committed. The host verifies runtime attestation before sending the bounded
request, executes independent `PRIMARY` and `REPLAY` lanes, and fails closed on
any hash, capability, output, or determinism mismatch.

This command is a runtime/ABI check only. Its output is not falsification
evidence and cannot transition a Challenger into OOS or shadow. The registered
Candidate remains `PROPOSED` until the trusted data producer has supplied
point-in-time scenarios with matured outcomes and every lifecycle gate has
accepted its append-only artifacts.

## Synthetic smoke test

The public fixtures contain no real account state:

```powershell
uv run python -m trading.cli seed demo
uv run python -m trading.cli replay --run-id demo_run
uv run python -m trading.cli verify --run-id demo_run
```

Re-running the same deterministic input must return the same hashes and must not
duplicate economic effects.

## Research configuration and status

Validate and inspect the common contracts:

```powershell
uv run python -m trading.cli config validate --all
uv run python -m trading.cli research schema
uv run python -m trading.cli research status
```

### Research scheduler

Run the Research Plane scheduler in its own process or host-level service. It is
independent of the regular-session Operational Trading Plane and never imports or
calls a broker, WebGPT, or Codex:

```powershell
uv run python -m trading.cli research schedule-plan
uv run python -m trading.cli research schedule-work `
  --worker-id research-scheduler-01
```

`schedule-plan` uses only versioned `market_calendar_sessions` rows for completed
session work. Normal and early closes therefore share the same rule, and a
holiday with no session row creates no daily work. Weekly work is calculated in
the configured IANA timezone, including DST. Evidence-triggered work is created
only after the configured number of globally unique, previously unconsumed
content hashes is available.

`schedule-work` first plans due work and then creates one typed, append-only
dispatch receipt. It does not execute the research model itself. Run it at the
configured `worker_poll_seconds` cadence through the local process supervisor.
PostgreSQL claims use the database clock, row locks, lease tokens, and attempt
fences. A reclaimed or expired worker cannot append a receipt or outcome.
Failures and retries are append-only events; raw exception detail and credentials
are never persisted. Inspect `scheduler` in `research status` or the Research UI
before operating the downstream aggregation/deep-research consumer.

### Recursive outcome ledger (Phase 0 and PR 1)

Migration `0014_experiment_outcome_ledger` adds immutable experiment actions,
per-experiment outcome-event hash chains, and point-in-time research-memory
snapshots. The feature remains disabled; normal scheduler planning therefore
does not create recursive-maintenance work.

The scheduler contract reserves this order for a later enabled implementation:

```text
DAILY_AGGREGATION
→ OUTCOME_MATURATION
→ RESEARCH_MEMORY_MATERIALIZATION
```

Each successor waits for the predecessor's append-only `SUCCEEDED` event. A
dispatch receipt does not execute maturation or memory materialization, and no
production consumer for those targets exists in PR 1.

Operators can inspect due experiments:

```powershell
uv run python -m trading.cli research outcome mature `
  --as-of 2026-07-28T00:00:00Z
```

### Deterministic Meta Controller (PR 2)

Migration `0015_meta_controller_v1` adds append-only action plans and accepted
V2 proposals. First persist a point-in-time memory snapshot, then build a plan.
The command is dry-run unless `--commit` is supplied:

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

The controller reads only the verified event prefix bound to the snapshot.
`PROMOTION_OOS`, `META_AUDIT`, future, unmatured, invalid, and legacy events do
not become reward samples. A committed plan authorizes only action kinds and
submission counts; it does not execute a Commander, Builder, promotion, or
trade. Inspect `recursive_improvement.meta_controller` in `research status`.

Validate a trusted host-produced outcome without writing:

```powershell
uv run python -m trading.cli research outcome mature `
  --input .local/research/outcome.json
```

Append only after reviewing the dry-run output:

```powershell
uv run python -m trading.cli research outcome mature `
  --input .local/research/outcome.json `
  --commit
```

Materialize memory with explicit point-in-time bounds:

```powershell
uv run python -m trading.cli research memory materialize `
  --as-of 2026-07-28T00:00:00Z `
  --data-available-cutoff 2026-07-28T00:00:00Z `
  --created-at 2026-07-28T00:00:00Z
```

This command also defaults to dry-run; add `--commit` to persist the immutable
snapshot. The CLI does not calculate economic outcomes, register actions
automatically, feed memory to a model, promote a Challenger, or create orders.
See [Recursive improvement](research/recursive-improvement.md) and
[Experiment outcome ledger](research/experiment-outcome-ledger.md).

### Portfolio-level Delta-Sharpe and OOS V2 (PR 3)

Migration `0016_portfolio_delta_sharpe_v2` stores one append-only
`PortfolioComparisonContractV1` per registered Candidate artifact. Create and
persist that allocation/integration contract before reserving OOS budget.
Creation after an OOS reservation or result fails closed.

The production V2 lockbox uses a fresh worker and private-root dataset exactly
like V1, while binding the full-portfolio comparison contract. It returns only
bounded aggregate Sharpes, DeltaSharpe confidence bounds, cost-stress results,
reason codes, and hashes. Never copy private rows or raw bootstrap samples to
logs, the database, UI, Commander, Builder, or a public artifact.

`research status` reports
`recursive_improvement.portfolio_delta_sharpe.status=IMPLEMENTED_DISABLED` and
the immutable comparison-contract count. There is intentionally no CLI that
lets an operator inject daily returns. OOS production calls must use the
trusted Candidate evaluation producer and `OosLockboxServiceV2`.

Promotion V2 independently requires positive configured OOS and shadow
DeltaSharpe lower bounds plus the worst-cost gate and all legacy risk,
capacity, replay, and falsification gates. It can only produce eligibility;
manual approval and explicit Champion designation remain separate.

### Chronological Meta-OOS (PR 4)

Migration `0017_chronological_meta_oos_v1` stores immutable plans, protected
dataset reservations, bounded epoch-arm audit hashes, and aggregate results in
an isolated append-only namespace. The four mandatory arms are
`STATIC_CHAMPION`, `FIXED_RECALIBRATION`, `MEMORYLESS_COMMANDER`, and
`ADAPTIVE_META_CONTROLLER`.

Validate and persist a predeclared plan:

```powershell
uv run python -m trading.cli research meta-oos plan `
  --input .local/research/meta-oos/plan.json
uv run python -m trading.cli research meta-oos plan `
  --input .local/research/meta-oos/plan.json `
  --commit
```

Inspect readiness, then explicitly reserve the protected dataset:

```powershell
uv run python -m trading.cli research meta-oos run --plan-id <PLAN_ID>
uv run python -m trading.cli research meta-oos run `
  --plan-id <PLAN_ID> `
  --idempotency-key <UNIQUE_KEY> `
  --commit
```

The CLI never accepts raw audit returns. The private trusted service runs the
predeclared plan and persists only the aggregate boundary. Verification is
read-only:

```powershell
uv run python -m trading.cli research meta-oos verify --plan-id <PLAN_ID>
```

`recursive_improvement.meta_oos` remains
`IMPLEMENTED_DISABLED`; synthetic fixtures validate implementation, not alpha.
See [Chronological meta-OOS](research/chronological-meta-oos.md).

Select exactly one Research Commander with optimistic version checking:

```powershell
uv run python -m trading.cli research select `
  --commander CODEX_SOL_MAX `
  --expected-version 0
```

Use `WEBGPT_SOL_PRO` to select the WebGPT Commander instead. A later selection
increments the append-only version and invalidates outstanding requests from the
previous selection.

Create all file artifacts under a Git-ignored local root. Repository-local output
outside `.local/`, `artifacts/`, `runs/`, or `data/raw/` is rejected. External
absolute paths are treated as local operator storage and are never printed in
full.

Run the active Web Scout from a versioned request:

```powershell
$env:TRADING_REAL_LLM_ENABLED = "true"
uv run python -m trading.cli research scout `
  --request .local/research/input/web-scout-request.json `
  --output .local/research/evidence/evidence-bundle.json
```

The command uses only headed Chrome, loopback CDP, AGBrowse, and the verified
GPT-5.6 Sol Pro/xhigh route configured in the local environment. It fails closed
instead of falling back. Its output contains a bounded hash/count receipt rather
than credentials, prompts, or raw browser payloads.

Prepare and persist a Commander cycle:

```powershell
uv run python -m trading.cli research cycle-prepare `
  --request .local/research/input/research-request.json `
  --bundle-root .local/research/runs
```

Import the Scout evidence after the cycle exists:

```powershell
uv run python -m trading.cli research evidence-import `
  --request .local/research/input/research-request.json `
  --evidence .local/research/evidence/evidence-bundle.json
```

Import a selected Commander result. The catalog and evidence must be the exact
hash-bound versions referenced by the request:

```powershell
uv run python -m trading.cli research decision-import `
  --request .local/research/input/research-request.json `
  --decision .local/research/output/research-decision.json `
  --catalog .local/research/input/available-data-catalog.json `
  --evidence .local/research/evidence/evidence-bundle.json
```

After the separate Candidate Builder has produced a versioned manifest, register
it against the already accepted proposal:

```powershell
uv run python -m trading.cli research challenger-register `
  --decision .local/research/output/research-decision.json `
  --manifest .local/research/output/challenger-manifest.json
```

Registration compares the proposal hash, strategy and parent versions,
hypothesis, Commander, evidence IDs, data requirements, horizon, execution
universe, turnover, and capacity. A mismatch is rejected before the append-only
Challenger row is created. These commands do not invoke Codex Builder themselves;
that invocation remains isolated in the separate Research Commander repository.

## UI

Start the loopback UI:

```powershell
uv run python -m trading.cli ui serve --host 127.0.0.1 --port 8765
```

Both UI serve commands reject non-loopback hosts. They are local operator
surfaces and must not be exposed through a custom ASGI runner or reverse proxy
without a separately reviewed authentication boundary.

The Research tab reports:

- current Champion and mutation policy;
- selected Commander and selection version;
- Web Scout model/reasoning/access requirements;
- catalog asset classes;
- recent cycles and evidence;
- proposals and Challengers;
- OOS, immutable shadow summaries, trusted promotion evaluations, manual
  approvals, and Champion designation history;
- durable 2×2 AI/guard arm NAV, cash, positions, pending orders, and fills;
- daily factorial schedule, replay/matched-condition state, common-session
  progress, and Guard/AI/interaction readiness;
- publication records;
- `real_order_routing=false`.

A blank section means no accepted record exists; it is not evidence that a stage
passed.

The machine-readable factorial view is:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/api/research/factorial/status
```

`NOT_INITIALIZED` means no durable factorial paper run exists.
`BLOCKED_MATCHED_CONDITIONS` means the stored run cannot be compared safely
because its configuration, schedule, market/forecast input, execution scenario,
cost model, capital, or append-only materialization does not match. Do not use
preliminary effects from a blocked run. `SHADOW_RUNNING` means replay and
matched-condition validation passed; effect readiness remains false until the
configured minimum common sessions have accumulated.

## Daily operational checklist

1. Confirm configuration and migration revisions.
2. Check market calendar, data freshness, and point-in-time completeness.
3. Confirm `real_order_routing=false` and production unlock disabled.
4. Verify active paper run identity, algorithm version, config hash, and code
   version.
5. Inspect reconciliation, pending orders, risk state, and settled/unsettled
   cash.
6. After the session, verify ledger/NAV and deterministic replay.
7. Aggregate bounded performance and failure clusters for later research.

Legacy and Q1 run procedures are in
[Forward paper operations](forward-paper-operations.md) and
[Q1 mathematical core](q1-math-core.md).

## Weekly research checklist

1. Freeze `as_of` and `data_available_cutoff`.
2. Build the versioned available-data catalog.
3. Aggregate current Champion, Challenger, performance, failure, regime, cost,
   capacity, and experiment-budget summaries.
4. Start a fresh Web Scout conversation and validate model, reasoning, browser,
   request, conversation, and completion bindings.
5. Persist only a valid evidence bundle; keep raw licensed payloads in ignored
   local storage.
6. Create a hash-bound `ResearchRequestV1`.
7. Invoke only the selected Commander in a fresh context.
8. Validate the decision, selection version, expiry, evidence IDs, and catalog.
9. If a proposal is accepted, invoke a separate fresh Candidate Builder.
10. Inspect patch paths and hashes before registering a Challenger.
11. Run mandatory falsification, replay, and then OOS in that order.
12. Start an independent matched shadow arm only after OOS `PASS`.
13. Materialize the matched shadow summary from immutable daily evidence.
14. Run trusted promotion evaluation from persisted falsification, OOS, replay,
    shadow, artifact, and current-Champion evidence.
15. If eligible, record explicit human approval.
16. Designate the new Champion only through a separate explicit human command
    with the expected current-version fence; never auto-promote.

## Failure recovery

| Symptom | Required response |
| --- | --- |
| WebGPT model/reasoning mismatch | Discard output; correct local selection; start a new conversation |
| Interrupted or incomplete WebGPT answer | Discard output; use a new request attempt |
| AGBrowse/CDP unavailable | Block Scout/Commander lane; do not fall back to API |
| Stale Commander selection | Create a new request bound to the current selection |
| Context/output hash mismatch | Quarantine artifact and investigate; never patch hashes |
| Codex process failure | Preserve sanitized failure; start a fresh process and directory |
| Forbidden candidate path | Reject candidate; use human-reviewed development for infrastructure |
| Mandatory test failure | Preserve failure; do not run OOS or shadow |
| OOS failure | Mark `OOS_REJECTED`; new tuning is a new hypothesis/submission |
| Promotion evidence mismatch | Reject evaluation; rebuild from immutable persisted evidence |
| Stale Champion version | Reject designation; refresh status and require a new explicit human decision |
| Ledger or replay mismatch | Stop affected paper lane and reconcile append-only records |
| Public release scan failure | Do not push; remove the source of exposure and rescan |

## Public release

Before any public push:

```powershell
uv run python scripts/public_release_scan.py `
  --root . `
  --expected-repository story7077/adaptive-llm-quant-public
```

Then run the complete tests, lint, type checks, migration
upgrade/downgrade/re-upgrade in a disposable database, replay, and ledger
verification required for the release.

The scanner must confirm:

- no credentials or credential-shaped tokens;
- no real balances, quantities, account identifiers, or personal data;
- no user-home or machine-specific paths;
- no `.env`, `.local`, raw payloads, browser profiles, or unexpected binaries;
- a new clean Git root with no inherited private history.

Failure is release-blocking. See
[Public release security](public-release-security.md).

## Operational limitations

- Paper fills cannot reproduce actual market impact, queue position, full
  latency, or every fee.
- Free or single-venue data is unsuitable for proving live execution quality.
- Web sources have varying availability and licensing; committed excerpts are
  bounded and provenance-bound.
- A Challenger passing OOS or shadow does not guarantee future alpha.
- Promotion eligibility still requires explicit human review and approval.
- Real broker routing remains unavailable.
