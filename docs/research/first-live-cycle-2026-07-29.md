# First Live Research Cycle — 2026-07-29 KST

This report records the first tool-backed Research Plane cycle and the
corresponding synthetic Q1 paper-runtime bring-up. It is an engineering and
provenance report, not evidence of profitability or statistical significance.
All timestamps below are UTC unless explicitly labelled otherwise.

## Outcome

The cycle completed these stages:

```text
Web research
→ structured evidence
→ isolated Research Commander decision
→ versioned Algorithm Proposal
→ isolated Candidate build and tests
→ immutable Challenger registration
```

The resulting Challenger remains `PROPOSED`. It has not entered mandatory
falsification, the locked OOS service, or an independent Challenger shadow arm.
Automatic promotion remains disabled. The separate operational
`q1_math_core_v1` paper runtime is live with a synthetic account, has captured
the completed prior-session adjusted inputs, and is waiting for the next
observed market session; it does not run this unvalidated Challenger.

## Research cycle identity

| Field | Value |
| --- | --- |
| Research cycle | `live-research-20260728t140210z-evidence3` |
| Request | `research-request-20260728t141926z-evidence3-v2` |
| Selected Commander | `CODEX_SOL_MAX` |
| Source snapshot | `ed4d42600f1053fbf57d0bd4e3d65c10d52bcaae` |
| Context manifest | `fd8862afba197ced8204abd2ef787b92fed86c2e49d91b69f40e05d67ea0575d` |
| Evidence bundle | `3e91ead710f4574f1043ad69a0a2461c4bf413a8910fccc4a1c4fd6140c6bc8e` |
| Commander decision | `835a8e1c8ba7385e3a5ed00c8590a632be6cd4f7c386097cc8e32f41be6c2cb0` |
| Proposal | `proposal-q1-det-diversifying-sleeve-v2.0.0` |
| Proposal hash | `fe9e70fe16aa877bb4164c92bcadb2fa95fc9b1bf2d81f958988506efd69dd42` |

The Web Scout used a fresh ChatGPT conversation through headed Chrome, CDP,
and AGBrowse. Preflight and postflight both verified `GPT-5.6 Sol Pro` with the
`xhigh` reasoning profile. The bounded result contained 14 sources, 14
structured claims, and 16 active search queries. Social evidence was not used
as a standalone fact.

The Research Commander ran as a fresh ephemeral `gpt-5.6-sol` invocation with
reasoning profile `max` in the separate Commander repository. It returned a
schema-valid proposal for `Q1-DET 2.0.0`, parented to `Q1-DET 1.0.0`. The
proposal adds a capped, independently gated GLD/TLT/SGOV residual sleeve while
preserving the parent QQQ/SOXX targets.

### Commander timeout-adoption audit

The retained host stderr records an initial rejection because that validator
did not yet collect evidence IDs nested inside the bounded Web Scout bundle.
The Codex child later exited, and the final execution record used the explicit
`HOST_ADOPTED_AFTER_SUPERVISOR_TIMEOUT` path with child exit confirmed.

A read-only historical revalidation with the current committed validator
accepted the published decision against the original request at its recorded
creation time. All 12 cited evidence IDs are members of the original 14-source
bounded request. Comparing the raw model output with the published decision
found differences only in the three trusted-host fields `created_at`,
`output_hash`, and `proposal.proposal_hash`; no economic hypothesis, rule,
scope, or evidence citation was changed during adoption.

## Candidate artifact

| Field | Value |
| --- | --- |
| Challenger | `challenger-c0bb5e7ebe50e442a6e39250` |
| Strategy version | `Q1-DET 2.0.0` |
| Candidate bundle | `8abe061a438043d6889f7205720c023bae916b508d6d7e3a1b79ba66434cf4c3` |
| Patch hash | `4abeab57db1dfeec6c179db11a93c8583cb8c1f51d996d24ff30a45083dbdea6` |
| Challenger manifest | `2b16115ace9dccf20c1db447bb43e032fa232c8a228a9b90e44b1720c26c8b16` |
| Candidate test manifest | `6cc62f2f01c975a4caa9c953d25f125144d626b08a3e79fb9f4bb452432bb343` |
| Current status | `PROPOSED` |

Commander and Builder were separate fresh invocations. The Builder received
only the approved structured proposal and sealed input bundle. Its host ABI
test plus 12 Candidate tests passed. The exact sealed Candidate files were
copied into the public repository and their SHA-256 hashes were verified before
registration.

The Candidate cannot access the network, credentials, broker, order, fill,
return, or P&L interfaces. `broker_access_permitted=false`,
`credential_access_permitted=false`, and `real_order_routing=false` are part of
the registered artifact.

### Typed discovery registration

Public PR
[#13](https://github.com/story7077/adaptive-llm-quant-public/pull/13)
connected a sealed V2 Candidate to the existing typed experiment-outcome
ledger without changing its lifecycle state. Commander PR
[#7](https://github.com/story7077/adaptive-llm-quant-research-commander/pull/7)
made the verified Candidate test manifest available as a sanitized, hash-bound
output for future cycles. Their merge commits are
`f702bd9143b99c04f810f7f264b462538924f266` and
`b422e467b0955e7dd8d475d1352c0226858f220c`, respectively.

Before the first registration, the read-only Alpaca calendar request returned
87 sessions through 2026-11-30. It appended 65 new PIT observations and left
22 identical observations unchanged. The source manifest hash was
`55292472cc6f9fec3b064cdc6e6dfbfca5044e46a02c5d948f5501503287608b`;
the command created zero schedules and zero orders.

At the database-clock decision time
`2026-07-28T23:39:12.758013Z`, the sealed Candidate became experiment
`candidate-discovery-experiment_59c4a93864a27828050d462f`. The immutable
records are:

| Record | Hash |
| --- | --- |
| Discovery action | `694839550c142d26d4b78ce151c463560e43035cef25f6a457f4fc92912275b5` |
| Registration event | `eb5c09daacbcc98cf315d2f6a265816acb8dc3a50443a6ab0d4a570820945533` |
| Technical-success event | `f29dd2c32668537b5f88126fb58f80b0bb666f3d08e57fa8e24812bc971f4c36` |

The maturity boundary is the close of the 63rd full future versioned market
session, `2026-10-26T20:00:00Z`. Repeating the registration returned the same
three hashes with all three `*_created` fields false. The resulting ledger has
one action, two effective events, and zero learning-eligible events.
`information_role=DISCOVERY`, `meta_training_permitted=false`, and
`eligible_for_meta_training=false`; the Challenger remains `PROPOSED`, with no
falsification pass, locked OOS result, shadow registration, promotion decision,
or order-routing capability.

## Why the Challenger is not in shadow

The downloaded adjusted daily history is useful for discovery, but it was
captured after the historical sessions. It does not establish the historical
revision and `available_at` provenance required for locked OOS or promotion
evidence. Treating it as historical point-in-time data would manufacture
knowledge the system did not have at each old cutoff.

The discovery-only matched replay covered 1,385 common sessions. At the base
cost model, `Q1-DET 1.0.0` had a Sharpe ratio of `0.67067` and `B0-VOL` had
`0.79015`, a delta of `-0.11948`. The result claims neither profitability nor
statistical significance and is not promotion evidence.

Current lifecycle counts for the new Challenger are:

- typed discovery actions: 1;
- effective discovery events: 2;
- learning-eligible events: 0;
- mandatory falsification reports: 0;
- locked OOS results: 0;
- Challenger shadow registrations: 0;
- promotion-eligible Challengers: 0.

The next valid step is prospective PIT collection followed by the declared
falsification suite. A mandatory failure must reject the Candidate before OOS
or shadow. At least 126 common out-of-sample sessions are required before any
manual promotion recommendation.

### Versioned prospective evidence producer

The follow-up `candidate_prospective_v1` path is intentionally not an
independent shadow arm. It binds one completed parent `Q1-DET` strategic
decision to one sealed Candidate request using:

- the parent's actual creation time as both decision time and signal cutoff;
- 220 aligned completed sessions for GLD, QQQ, SGOV, SOXX, and TLT;
- exact source bar IDs, event and availability times, and payload hashes;
- the common evaluation anchor and parent decision/input-manifest hashes;
- a versioned feature/config contract;
- the finalized Candidate artifact, aggregate config hash, and exact approved
  strategy-config content hash; and
- independent network-denied `PRIMARY` and `REPLAY` executions.

The first request starts from cash at the common evaluation anchor. A later
request may use only the prior verified Candidate target state. The append-only
tables introduced by migration `0018_candidate_prospective_v1` store requests
and execution evidence separately so a failed Candidate process can be retried
without rewriting the request. Immutable `request_recorded_at` and
`execution_recorded_at` values come from the database clock and remain separate
from the parent-bound logical decision/cutoff time.

This path never creates orders, fills, ledger postings, NAV, returns, a
Challenger lifecycle transition, or a shadow registration. Even a successful
response is explicitly `IMMATURE_FORWARD_ONLY`.

### Prospective outcome producer

Migration `0019_candidate_prospective_outcomes_v1` adds the missing future
outcome half of that bridge without changing the Challenger state. For each
successful request it precommits to a D+1 implementation close, a D+2
evaluation close, and a fixed D+2-close-plus-120-minute source cutoff. The
collector records exact adjusted-bar revisions, returns, parent and Candidate
weights, cost inputs, 20-session source-bound ADV, market and sector context,
known factor returns, and regime in one append-only record.

Records first observed after the fixed cutoff cannot change a past outcome.
If the monitor was unable to capture the required adjusted bars within the
window, it records an append-only
`PROSPECTIVE_OUTCOME_DATA_WINDOW_MISSED` terminal failure and does not invent
or retrospectively repair the observation. That failure contributes nothing to
readiness but lets the chronological collector continue to later requests.
Logical outcome creation is the deterministic cutoff time; the separate
database `recorded_at` retains actual insertion time, so worker retries and
restarts do not alter the outcome hash.

This producer still creates no Candidate arm, order, fill, ledger posting, NAV,
falsification report, OOS request, shadow registration, promotion event, or
Champion mutation. Its readiness threshold of 126 sessions and 504 instrument
observations means only that a later host-owned falsification dataset may be
assembled.

### Prospective evaluation dataset V2

The follow-up `candidate_prospective_evaluation_dataset_v2` path implements
that later host-owned assembly without changing the forward evidence already
recorded. Migration `0020_candidate_evaluation_dataset_v2` adds append-only
dataset and trace tables. The selection rule is the exact first 126 successful
forward sessions, with all terminal failures through the same selection cutoff
retained in the cohort manifest.

Every base and transformed scenario binds:

- the stored request and request-source manifest;
- the actual versioned market-calendar path;
- the deterministic transformation;
- the hidden forward-outcome source and availability time;
- the evaluation config and sealed Candidate artifact.

The predeclared variants cover adjacent parameter values, data removal,
calendar shift, signal placebos, and GLD/TLT label shuffle. Each variant evolves
its own target state chronologically. The future outcome is never placed in a
Candidate request. Independent isolated primary and replay invocations must
match before the host can persist the trace and run mandatory falsification.

The one-shot evaluation monitor remains dormant until the cohort is ready and
then stops after one terminal evaluation. It cannot request locked OOS, create
an independent shadow arm, promote the Challenger, access broker credentials,
or create an order. Until 126 successful sessions exist, its only valid state
is `WAITING_FOR_FORWARD_OUTCOMES`.

### Evaluation V2 deployment handoff

PR [`#17`](https://github.com/story7077/adaptive-llm-quant-public/pull/17)
merged as
`9d2dd8d0cadfc43c65b5847fdaf6ce4d668afa1d`. At
`2026-07-29T03:04Z`, the active PostgreSQL database was upgraded forward-only
from `0019_candidate_prospective_outcomes_v1` to
`0020_candidate_evaluation_dataset_v2`. No operational-database downgrade was
run.

The active Q1 process remains pinned to
`ad981ded63b14d90bb458c8db3759c00e8bcd819`; changing its checkout in place
would make its run code identity disagree with the already-created v5 run.
Three separate Research Plane monitors instead run from the merged `9d2dd8d`
tree:

- parent-bound Prospective Candidate target collection;
- precommitted forward-outcome collection;
- the dormant 126-session evaluation and falsification gate.

The older research monitors were stopped only after all three replacements
reported their running status with empty error logs. The Q1 process itself was
not restarted and its HTTP status remained available.

At `2026-07-29T03:11Z`, the authoritative merged-code CLI reported:

- `WAITING_FOR_PARENT_DECISION`, with zero requests;
- `ACCUMULATING_FORWARD_OUTCOMES`, with zero outcomes and zero terminal
  failures;
- `WAITING_FOR_FORWARD_OUTCOMES`, with `0/126` successful sessions;
- 18 predeclared evaluation variants;
- no evaluation dataset, trace, or Candidate runtime process;
- no OOS or independent Challenger shadow;
- automatic promotion disabled and `real_order_routing=false`.

The existing Q1 status endpoint reported `PENDING_BOOTSTRAP`, no evaluation
anchor, and the next `Q1_SETTLEMENT` cycle at `2026-07-29T13:30:00Z`. The first
eligible strategic decision remains `2026-07-29T14:00:00Z` (10:00 ET). Nothing
is backfilled before that decision.

The existing UI process is deliberately not hot-reloaded across the code
identity boundary. The merged CLI and monitor records are the current
Evaluation V2 authority until a new versioned Q1 UI process is started.
Machine-specific paths, process IDs, credentials, and local logs remain in
gitignored local operational metadata and are not part of this public record.

### Read-only Research status handoff

PR [`#19`](https://github.com/story7077/adaptive-llm-quant-public/pull/19)
merged as
`d4322edf89f2107f71ee9095ab31ec97c44fa29c`. It adds a loopback-only,
read-only status mode so the latest Research UI can observe the shared
PostgreSQL state without restarting the version-pinned Q1 process. This mode
starts neither a market-data worker nor a paper worker, writes no market
connection status, rejects every non-read HTTP method with `405`, and derives
external worker health only from the persisted runtime heartbeat.

At `2026-07-29T03:47Z`, the merged status surface and the original Q1 surface
reported the same v5 run identity. The external heartbeat was fresh, the Q1
state remained `PENDING_BOOTSTRAP`, and all three Research monitors remained
healthy. The Research projection reported:

- `WAITING_FOR_PARENT_DECISION`;
- zero prospective outcomes;
- `WAITING_FOR_FORWARD_OUTCOMES`, with 126 successful sessions required;
- no OOS request or independent Challenger shadow;
- automatic promotion disabled; and
- `real_order_routing=false`.

The status process was restarted from the merge commit only after the public
push and pull-request workflows passed. The operational Q1 process remained
pinned to its original code commit and was not restarted.

### Dormant OOS V2 and independent-shadow handoff

The public host now has an explicit dry-run-first path from a passed
whole-portfolio OOS V2 result to `SHADOW_PENDING`, a separately authorized
`SHADOW_RUNNING` transition, and an append-only matched Champion/Challenger
paper runtime. The path verifies the immutable Challenger, Candidate artifact,
mandatory falsification, deterministic replay, experiment budget, locked
private manifest, predeclared portfolio comparison, matched execution contract,
Champion portfolio manifest, and database time before accepting a mutation.

This is an operational handoff, not a claim that the live Challenger reached
the gate. For `challenger-c0bb5e7ebe50e442a6e39250`, the path has not been
invoked: forward outcomes remain below `126/126`, there is no falsification
pass or locked OOS result, and no independent shadow run exists. The status UI
therefore correctly reports `NOT_INITIALIZED`.

The matched shadow runtime has independent cash, positions, orders, fills,
ledger, NAV, state, costs, and replay for both arms. Its current V1 settlement
model uses same-cycle paper cash and exposes
`unsettled_receivables_supported=false`; it must not be described as a
production-broker buying-power model. Automatic promotion and real broker
routing remain unavailable.

### Trusted prospective-to-shadow provenance handoff

Public PR
[#22](https://github.com/story7077/adaptive-llm-quant-public/pull/22)
closed the remaining promotion-evidence provenance gap. It merged as
`eee2c40818cdcf0931250493261d9c24c0652277`; the corresponding
[public release workflow](https://github.com/story7077/adaptive-llm-quant-public/actions/runs/30427030217)
completed successfully.

A promotion-facing matched cycle can now be derived only from the exact
post-activation Q1 parent decision, sealed Candidate request, successful and
identical primary/replay response, registered Candidate artifact, fresh
persisted `CONNECTED` quotes, and completed PIT ADV bars. The host provenance
and both arm results are committed atomically. Pre-activation evidence is
ineligible, supplied performance summaries are recomputed from immutable
cycles, and manual JSON cycle commit remains disabled with
`UNATTESTED_MANUAL_SHADOW_CYCLE_COMMIT_DISABLED`.

At `2026-07-29T06:31Z`, three independent merged-code monitors were running:

- the target monitor was `WAITING_FOR_PARENT_DECISION` at `0` requests;
- the outcome monitor was `ACCUMULATING_FORWARD_OUTCOMES` at `0` outcomes;
- the evaluation monitor was `WAITING_FOR_FORWARD_OUTCOMES`;
- all three stderr logs were empty; and
- the independent shadow runtime correctly remained `NOT_INITIALIZED`.

The monitor's Candidate runtime attestation matched the registered bundle hash,
Candidate tree hash, aggregate config hash, CPython ABI, declared entrypoint,
and exact approved strategy-config hash. A separate read-only preflight
accepted 121 aligned QQQ/SOXX sessions for the Q1 signal and 220 aligned
GLD/QQQ/SGOV/SOXX/TLT sessions for Candidate features, both through the
completed 2026-07-28 session.

The version-pinned Q1 process was not hot-reloaded. Its heartbeat was current,
its next cycle remained the actual `2026-07-29T13:30:00Z` session open, and it
reported no runtime error. The Alpaca IEX stream was `CONNECTED`; stale prices
before the US session were expected and were not accepted as executable
quotes. Alpaca Paper canary, automatic promotion, and real broker routing all
remained disabled.

## Operational Q1 paper runtime

The initial bring-up run was the synthetic
`paper_q1_research_20260729_v5`.

At `2026-07-28T20:08Z` its server-reported state was:

- algorithm: `q1_math_core_v1`;
- Alpaca data feed: IEX, `CONNECTED`;
- coverage: single exchange, not SIP/NBBO;
- adjusted-history refresh: `READY`, with no refresh error;
- run state: `PENDING_BOOTSTRAP`;
- Q1 worker: `Q1_WORKER_RUNNING`;
- evaluation anchor: not yet established;
- next cycle: `Q1_SETTLEMENT` at `2026-07-29T13:30Z`
  (09:30 ET);
- Alpaca Paper order canary: disabled;
- real broker routing: unavailable and `false`.

An intraday-created run does not backfill a 09:30 decision with data observed
later in the day. It waits for the next market session whose calendar was
already available before the scheduled open. This is intentional PIT
fail-closed behavior.

Two runtime defects were found during bring-up and fixed:

1. Historical calendar sessions were incorrectly given executable schedule
   slots. Their retryable bootstrap starved the current queue. Calendar history
   is now persisted for PIT and settlement use, while runtime slots are created
   only for not-yet-open sessions.
2. A repeated fetch of the same stable calendar source ID could attempt a
   duplicate insert because the later observation time changed process
   provenance. Re-observation is now idempotent by stable source ID; conflicting
   market hours still fail closed.

The earlier diagnostic run rows and cycle records were retained. No append-only
record was deleted or rewritten.

### Post-close adjusted-history verification

Public PR
[#9](https://github.com/story7077/adaptive-llm-quant-public/pull/9)
added a bounded adjusted-history refresh projection to the market status API
and UI. Both public GitHub workflows passed, and merge commit
`fbde26a9fce67ca47eb2a26d247b5f5be399851a` is deployed locally.

The 2026-07-28 session closed at `2026-07-28T20:00Z` according to the stored
versioned market-calendar row. The first post-close refresh appended six daily
bar revisions. A later restart appended three more revisions, including a QQQ
late-volume revision. At `2026-07-28T20:15:15Z`, a deliberate confirmation
fetch appended seven additional provider revisions. The same history request
nine seconds later returned 1,054 bars and appended zero rows. This verifies
both provider-result stability at that observation time and append-only
idempotency; it does not claim that a data vendor can never publish a later
correction.

The exact prospective Q1 preflight for the next `10:00 ET` signal cutoff
returned:

| Field | Value |
| --- | --- |
| Signal cutoff | `2026-07-29T14:00:00Z` |
| Completed-session range | `2026-02-03` through `2026-07-28` |
| Aligned sessions | 121 |
| Source bars | 242 |
| Source-bar manifest | `8264b4f971d1b63f611d743ee54d4cd83548ca8a53e1deeeb7262b5117ec1d25` |
| All records available by cutoff | `true` |

The selected QQQ revision was available at
`2026-07-28T20:15:16.447182Z`; the selected SOXX revision was available at
`2026-07-28T20:05:57.761422Z`. Both carried `adjustment=all` and dataset
version `alpaca_iex_adjusted_all_v1`.

The first attempt to restart the merged code under the existing v4 run ID was
rejected with `Q1PAPERRUNCONFLICT: Q1 run code version changed`. This is the
intended run-identity fence: a run cannot silently change code version. The v4
records were preserved, and the merged code was started as the new v5 run
instead.

### Pre-open runtime snapshot correction

Before the first v5 cycle became due, a restart preflight found that the
long-running process still held its original Python modules while the source
worktree on disk had subsequently advanced with Research Plane merges. The
immutable v5 run was bound to workspace code identity
`workspace:07a76c0356d8bfe95b2c878c316ce35b55e29f705cb4511fc54898010911251d`,
whereas the advanced worktree produced a different identity. The existing run
therefore rejected an idempotent initialization attempt with
`Q1PaperRunConflict: Q1 run code version changed`.

Git-object reconstruction showed that the stored identity exactly matched the
code tree at merge
`fbde26a9fce67ca47eb2a26d247b5f5be399851a`. A dedicated detached runtime
snapshot and isolated environment were created from that merge. Its code
identity and Q1 config manifest matched the immutable run, and an idempotent
initialization returned `created=false`, `PENDING_BOOTSTRAP`, and
`real_order_routing=false`.

The old process was then replaced with the exact snapshot before the market
opened. Startup initially failed closed because a visible ChatGPT
acknowledgement modal blocked the model selector. The same headed Chrome and
AGBrowse control path identified and dismissed only that modal. The subsequent
bridge preflight verified `GPT-5.6 Sol` with `xhigh`, after which the Q1 API,
IEX stream, adjusted-history refresh, heartbeat, and Research monitors all
returned healthy.

No economic Q1 record existed before or after the replacement:

- evaluation anchors: 0;
- Q1 portfolio decisions: 0;
- order events: 0;
- Q1 NAV snapshots: 0;
- strategy daily results: 0; and
- non-pending cycles: 0.

The restarted worker extended the versioned 30-day calendar window to 9,637
pending slots across 23 actual sessions, from 2026-07-29 through 2026-08-28.
This was a schedule-only append. No past decision was synthesized or
backfilled. Alpaca Paper canary and automatic promotion remained disabled, and
real broker routing remained unavailable.

### Current v7 private-account paper handoff

Public PR
[#25](https://github.com/story7077/adaptive-llm-quant-public/pull/25)
moved PostgreSQL paper-cycle claims, lease reclamation, and claim timestamps to
the database clock. The merged main commit is
`00c726b9509ea60a3a058d77e8152e65bcff31d6`. A disposable PostgreSQL proof
showed that a host clock one hour fast could not claim before database due time,
while a host clock one hour slow could claim after database due time.

Before any economic row existed, the pre-open audit found that the then-current
v6 run still referenced the public synthetic example account. That run was
preserved and stopped with zero anchors, decisions, orders, fills, NAV
snapshots, or daily results. A new versioned run,
`paper_q1_research_20260729_v7`, was created from the same merged code and Q1
configuration but bound to the user-supplied Toss snapshot in local,
gitignored storage. Account cash, quantities, symbols, screenshots, and local
paths are intentionally absent from this public record. Frozen non-USD cash is
non-tradable. HOLD and LIVE-MIRROR will inherit the local snapshot at session
open; clean strategy arms will still start cash-only at the common T0 NAV.

The v7 immutable identities are:

| Field | Value |
| --- | --- |
| Algorithm | `q1_math_core_v1` |
| Source commit | `00c726b9509ea60a3a058d77e8152e65bcff31d6` |
| Code identity | `workspace:69ded580a1099368b50c6b9b6d9d2471150170aec613f1281f8a4462cff443f3` |
| Q1 config manifest | `afcaa7ea2939b3ca39ecae9f553794450ca498a0d1a48a19758eaab70479ad36` |
| Initial replay hash | `2943f560930e333c7ef3a2ff964876e1fe91f161f01cedd1899c3f8296aa83a7` |
| First session open | `2026-07-29T13:30:00Z` |
| First strategic cycle | `2026-07-29T14:00:00Z` |

Two independent pre-open replays produced the same initial hash and identical
checks. All available record-hash, row-consistency, state-machine, typed-risk,
and `real_order_routing=false` checks passed. Only
`initial_state_economics_valid` and `complete_session_record_set_present` were
false because no session-open state or complete session record existed yet.
This is an incomplete-stream result, not a successful economic replay.

The Q1 runtime, loopback-only status UI, and three prospective Candidate
monitors were moved to the same detached main snapshot and Python environment.
No process remained on an older feature worktree. A user-level local supervisor
now restores those five processes after logon or process failure without
publishing its machine-specific configuration. An intentional termination of
the evaluation monitor was recovered with new process IDs within one 30-second
poll. PostgreSQL is an automatic Windows service. The database, Q1 API, status
API, and Chrome CDP listeners were verified as loopback-only.

At the sanitized handoff checkpoint:

- v7 was `PENDING_BOOTSTRAP` with no runtime error;
- the IEX stream and headed AGBrowse/CDP browser were connected;
- the WebGPT bridge preflight was healthy with `xhigh`;
- target, outcome, and evaluation monitors were running against v7;
- the Candidate remained `WAITING_FOR_PARENT_DECISION`;
- forward outcomes remained `0/126`;
- independent shadow remained `NOT_INITIALIZED`;
- automatic promotion and broker access remained disabled; and
- `real_order_routing=false`.

No return, alpha, fill, or profitability statement can be made from this
pre-open checkpoint. The post-open anchor, decisions, order events, paper fills,
NAV records, and replay hash must be appended only after the actual scheduled
cycles occur.

### Research schedule activation

At `2026-07-29T09:21Z`, the existing append-only Research Scheduler service was
started as a sixth supervisor-managed process on the same detached main
snapshot. A read-only preview before activation found 32 due plans within the
configured 35-day planning window:

- 24 `DAILY_AGGREGATION`;
- 5 `WEEKLY_DEEP_RESEARCH`; and
- 3 `EVIDENCE_TRIGGERED_RESEARCH`.

The first scheduler tick persisted those 32 immutable plans and began appending
one fenced dispatch receipt per configured 60-second poll. The scheduler neither
imports nor calls a broker, WebGPT, or Codex. Recursive outcome maintenance,
automatic promotion, and real order routing remained disabled.

A dispatch receipt is not model execution. At this checkpoint no downstream
receipt consumer was running, so the activation proved automatic calendar
planning and fenced dispatch only. The later scheduled-consumer cycle below
closed that local execution gap while preserving the same leases, isolation,
and paper-only boundaries.

### Observed v7 close and v8 rollover

The v7 run reached the actual 2026-07-29 session and preserved its bootstrap
and risk-check records, but its first 10:00 ET strategic cycle never committed.
That cycle made 79 retryable attempts and ended with
`Q1_DATA_NOT_READY`; the last recorded detail was that the decision quote
bundle exceeded the configured maximum skew. The run contains six NAV
snapshots but zero evaluation anchors, portfolio decisions, order events,
strategy daily results, or risk episodes. No late decision was synthesized.

A separate first-decision risk-bootstrap ordering defect found during this
operation was fixed in public
[#30](https://github.com/story7077/adaptive-llm-quant-public/pull/30).
The fix excludes only pristine, uncommitted cash-only Q1 strategy arms from
pre-commit risk checks; LIVE-MIRROR and all subsequent checks remain active.
The v7 records remain unchanged.

The corrected code started a new versioned run,
`paper_q1_research_20260729_v8`, at source commit
`9875cad1a86a55a1dc03080b065de64fd640247f`. It is
`PENDING_BOOTSTRAP`, has no evaluation anchor or economic decision, and is
waiting for the next actual session open at `2026-07-30T13:30:00Z`. The first
strategic cycle is scheduled for `2026-07-30T14:00:00Z`; normal execution
slices, if any valid intents exist, follow from 14:01 through 14:20 UTC.
The worker heartbeat, IEX stream, and local WebGPT preflight are healthy.
Alpaca Paper canary and automatic promotion remain disabled, and
`real_order_routing=false`.

### First scheduled-consumer Research Cycle

The first deep-research dispatch consumer then completed an actual
Scout-to-Commander-to-Builder cycle from the append-only scheduler receipt.
It reused no prior conversation or Codex invocation.

| Field | Value |
| --- | --- |
| Execution | `research-work-execution_d6cc2889f9e6dea8363ec37e` |
| Research cycle | `scheduled-research-cycle_8a62e733ec30f116af96fe2f` |
| Selected Commander | `CODEX_SOL_MAX` |
| Source snapshot | `0cd5dbbcd5b484425cca0126569bbaf03d05d073` |
| Context manifest | `a6bc8bbaa78a737c300f139e676439964b395df0815e0544ff03ce5c773fafa1` |
| Decision | `PROPOSE_NEW_STRATEGY` |
| Decision hash | `0d5ec0a7618067c2eaf1bb4974bc544f173358ff8b1e1d23c6cb484f805bdd2a` |
| Proposal | `proposal-iwm-rate-credit-gate-v2.0.0` |
| Proposal hash | `61dc42efcb8de5f2a72f95fd8c9a539105b24d6215d83dcbb0523845986a2c2e` |
| Challenger | `challenger-4edf3f6b32a5f9e136f916f5` |
| Strategy | `IWM-RCG 2.0.0` |
| Result hash | `80337af9f9a0e0b8cebc0ee89d6c48289289f05c981b47ffc00d9527cd6c82f2` |

The fresh Web Scout conversation used headed Chrome, CDP, AGBrowse,
`GPT-5.6 Sol Pro`, and `xhigh`. It completed 12 active queries and returned 17
sources plus 17 structured claims. Social sources remained leads rather than
standalone facts.

The Research Commander ran in the separate repository as fresh ephemeral
`gpt-5.6-sol` with reasoning `max`. It proposed a rate-and-credit-confirmed IWM
strategy over `HYG`, `IWM`, `QQQ`, `SGOV`, `SPY`, and `TLT`. A second, isolated
Builder invocation produced only the approved versioned patch and declared
tests. Its supervisor timed out after the child had already exited; the host
used the explicit `HOST_ADOPTED_AFTER_SUPERVISOR_TIMEOUT` path, confirmed the
child exit, stability window, output hash, candidate tree, and ACL cleanup, and
did not relaunch the model.

The host test run collected 25 tests: 24 passed and one failed. The failure was
a strict floating-point equality assertion in a sleeve-allocation regression.
The system did not reinterpret that as success. It preserved the patch,
Challenger manifest, and structured test attestation, then appended the typed
`CandidateTestFailureV1` event and terminal `TEST_FAILED` status.

| Failure record | Hash |
| --- | --- |
| Challenger manifest | `9c5ab83966b7957b3203e87b6e521a6947e1a7f3fea9849cdb49112ac5edcf11` |
| Candidate test manifest | `74d0848c47b1a066e2fcecc868a47091bf131df1ddc0aef5330b82ffe3fe6a49` |
| Candidate test failure | `09ecdab4c8c573c7c7f60bee9cd34448deb4fcae6320a7459f2cdd81ed0e3e6d` |

Commander public
[#11](https://github.com/story7077/adaptive-llm-quant-research-commander/pull/11)
and trading public
[#32](https://github.com/story7077/adaptive-llm-quant-public/pull/32)
added the failed-Candidate preservation and trusted lifecycle gates. Their
merge commits are `e1493853d9fcf142d992195971b6e6d345591156` and
`fabd3ff2502698cc6a6fcde77e56ccdd1652cffe`.

The rejected Challenger has zero Candidate artifacts, falsification reports,
OOS results, and shadow-arm registrations. Replaying the same execution request
returned `RESULT_ADOPTED`; the Challenger event count remained one and the
Commander and Builder invocation counts remained one each. Automatic
promotion and real broker routing stayed false.

### v8 pre-session replay and Research retry handoff

Public PR
[#33](https://github.com/story7077/adaptive-llm-quant-public/pull/33)
merged as `4ec1a2c30dca1540773c61dcef7e4e699d9fe919`. It removed an
ambiguous status fallback that could project the newest unrelated Challenger
before the first prospective request existed. The status CLI and read-only UI
now resolve the unique configured strategy/version that also has a sealed
Candidate artifact, or fail closed when the binding is missing or ambiguous.

The deployed status surface consequently binds target, outcome, and evaluation
monitoring to `challenger-c0bb5e7ebe50e442a6e39250`. It reports
`ACCUMULATING_FORWARD_OUTCOMES`, `WAITING_FOR_FORWARD_OUTCOMES`, `0/126`
successful sessions, and `NOT_INITIALIZED` for the independent shadow runtime.
The rejected IWM Challenger remains independently visible as `TEST_FAILED`;
none of its records were modified.

At `2026-07-29T17:43Z`, two read-only v8 replay invocations and one verify
invocation returned the same deterministic hash:

`0d6154969e25b922de1913ff72f590d71784da0958ef2075e4de9fa9678af340`

All three results were `INCOMPLETE_EVENT_STREAM`. Only
`initial_state_economics_valid` and
`complete_session_record_set_present` were false because the run had not yet
reached its scheduled session bootstrap. This is the expected pre-bootstrap
state, not a successful completed-session replay. The next session bootstrap
remained `2026-07-30T13:30:00Z`, followed by the first strategic cycle at
`2026-07-30T14:00:00Z`.

The pending Research retry remains a fenced second attempt. Its latest
read-only headed-Chrome/AGBrowse bridge preflight returned
`provider_temporarily_rate_limited` before any prompt or model invocation. A
supervised local watcher polls the same fail-closed preflight and may claim the
specific append-only receipt only after the bridge again verifies the required
model and reasoning profile. It does not use an API fallback, a different
ChatGPT model, automatic promotion, or broker routing.

### Native Commander recovery and v9 paper handoff

Public PR
[#34](https://github.com/story7077/adaptive-llm-quant-public/pull/34)
raised the public-release workflow timeout from 10 to 30 minutes after a
complete public suite legitimately exceeded the former limit. Both required
checks passed and the PR merged as
`2f649d5265ef8f24034037d099d4cbdb767c699e`.

The next retry reached the WebGPT postflight but failed before Codex started.
The public dispatch consumer had correctly removed model-visible host identity
variables, but it also removed the three host-only values that the Windows ACL
controller needs to construct the native read jail. Public PR
[#35](https://github.com/story7077/adaptive-llm-quant-public/pull/35)
now permits `COMPUTERNAME`, `USERNAME`, and `USERDOMAIN` only in the trusted
executor environment. The child model environment still excludes all three,
as well as `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, credential-shaped
variables, and Codex/OpenAI environment variables. The focused integration
test requires the exact host identity values while proving that an injected
Alpaca secret cannot reach the executor. Both full public CI checks passed and
the PR merged as
`1c330a2246e437db04f8eb6526a7b8589a325973`.

The fenced retry execution
`research-work-execution_a4fc34b006eaef47cb8568ab` then completed a fresh
headed-Chrome WebGPT scout conversation. It produced
`research_evidence_bundle_v1` for cycle
`scheduled-research-cycle_1a3efe10275ac9748967d419`, bound to:

- WebGPT request `scheduled-web-scout_a58890bdf20446c4cf9046f2`;
- conversation `6a6a4b82-830c-83e8-8ca0-792dab20360b`;
- browser session `320dcdc9-ee35-4f20-80bd-399804592126`;
- `GPT-5.6 Sol Pro` with `xhigh` reasoning;
- 13 accepted sources: seven `TIER_1_OFFICIAL` and six
  `TIER_2_PRIMARY_DATA`; and
- source snapshot commit
  `1c330a2246e437db04f8eb6526a7b8589a325973` with context manifest
  `3cc85144c5a1251aec8c2f8aab20db8ffbf75ac87a059cbc6dfe3da12cb1dea5`.

The selected Commander is `CODEX_SOL_MAX`. Invocation
`commander-44a1ee01f2ea43e8a9f4634770d2cf2a` started in the separate public
Commander repository with `gpt-5.6-sol`, reasoning `max`, a fresh process,
`--ephemeral`, `--ignore-user-config`, disabled memories/plugins/apps/browser
and computer use, no resume, no persistent history, and a schema-bound output.
The run emitted `native-acl-events`, `native-read-jail-preflight.json`, and
`execution-started.json` before model execution. The Builder has not been
conflated with this invocation; it must receive a separate invocation record
only after a valid structured proposal.

The invocation completed with the schema-valid decision
`REQUEST_MORE_EVIDENCE`, not a fabricated proposal. The Commander found that
Q1-DET 1.0.0 underperformed B0-VOL under all three bound cost stresses, while
the already registered Q1-DET 2.0.0 Challenger still lacked a hash-bound
discovery/falsification result. It also rejected a liquidity/credit mechanism
because the request did not contain release-lagged historical H.4.1 or
high-yield OAS data. The four requested evidence classes were:

- discovery evaluation of Q1-DET 2.0.0 with its declared placebos, ablations,
  regime splits, cost/delay stresses, turnover, capacity, and portfolio
  DeltaSharpe;
- release-lagged, revision-aware H.4.1 history;
- release-lagged high-yield OAS history, or a predeclared discovery comparison
  showing whether eligible HYG adjusted bars are an adequate proxy; and
- the sanitized structured test-failure detail for IWM-RCG 2.0.0.

The decision output hash is
`5c0210a10f8c67d0163fed6c5704baf1b2beac36b13c059dfefa341da7443df0`.
The dispatch result hash is
`067dbed986a0d40d38512530796d8d6dddc820911644832ba09dcbdaa4ee9780`.
Scheduler work `research-work_3ff291034f967491cb15129b` attempt 2 appended
`SUCCEEDED`; the result attests one fresh Web Scout invocation and one fresh
Research Commander invocation. No Builder invocation, Candidate patch,
falsification result, OOS request, or shadow registration was created because
there was no approved proposal.

The status API initially returned HTTP 500 because the detached v8 paper
runtime predated execution-lease scheduler events that already existed in the
append-only database. Current public main already contained the event
projection and integration coverage. Deploying it against the old run ID
correctly failed closed with `Q1PaperRunConflict: Q1 run code version changed`.
The v8 run remains immutable and readable. A new
`paper_q1_research_20260729_v9` run was therefore started at the current main
commit. Its paper and Research status endpoints both returned HTTP 200, the Q1
worker resumed with a fresh heartbeat, the next calendar-backed bootstrap
remained `2026-07-30T13:30:00Z`, and `real_order_routing` remained false.

The four local Research monitors and the loopback read-only status process
were also moved from the immutable v8 identity to v9 before the next market
session. The target collector remains `WAITING_FOR_PARENT_DECISION`, the
outcome collector has no eligible request, and the evaluation gate remains
`WAITING_FOR_FORWARD_OUTCOMES`. This changes no stored v8 record and does not
backfill a Candidate observation.

The host supervisor still carried the earlier detached-runtime commit as its
restart preflight even though the live children had already moved to current
main. That would have prevented the complete stack from returning after a
supervisor or host restart. Its expected commit was updated to
`1c330a2246e437db04f8eb6526a7b8589a325973`, and the supervisor alone was
restarted. It logged `SUPERVISOR_STARTED` with the new commit while the
existing v9 worker retained its heartbeat, next calendar cycle, and HTTP
availability. The receipt-specific retry watcher for the now-successful
attempt was removed from the supervisor set and stopped; the generic fenced
consumer remains responsible for future eligible work.

Two independent pre-bootstrap Q1 replays of v9 returned the same hash,
`56a8f2347e84279f88b2a24fbf56cd0ae0919d4dd9439b6c0f85ede81253090e`.
Both correctly returned a nonzero exit and
`complete_session_record_set_present=false` because no common T0 anchor or
completed session exists yet. The available run-version and routing checks
passed, including `run_is_q1_math_core_v1=true` and
`real_order_routing_false=true`. This is deterministic incomplete-stream
evidence, not a completed-session replay claim.

One of the four Commander evidence requests was then resolved without changing
the failed Candidate. An exact, network-denied replay of the sealed IWM-RCG
2.0.0 Candidate test file reproduced one failure:

`tests/candidates/test_iwm_rcg_v2_0_0.py::IwmRcgCandidateTests::test_host_owned_sleeve_variants_preserve_full_investment_limit`

The observed assertion was the strict binary floating-point comparison
`0.19999999999999996 != 0.2`; the isolated Candidate test file reported 23
passes and one failure. The original host manifest remains authoritative at 25
collected checks, 24 passes, and one failure because it also includes the
host-owned ABI check. A bounded structured diagnostic binds the original
Candidate manifest, Candidate tree, test manifest, failure record, exact test
file, implementation file, and strategy config. Its diagnostic hash is
`bb38b2ef50b5559a432edc16275626231f8b8b3a0dcfbf5dea90d6fb7183c6e9`.
It stores neither raw process output nor local absolute paths and is included
as a distinct failure-evidence cluster in the next Research request. The
existing IWM-RCG 2.0.0 result remains immutable `TEST_FAILED`; any retry must
use a new Candidate version.

### Commander-requested evidence follow-up

A separately bound follow-up cycle,
`requested-evidence-followup-cycle_1b65d456768d0d0da3002369`, used a new
WebGPT conversation rather than resuming any prior Scout or Commander context.
The headed-Chrome postflight verified:

- request `requested-evidence-web-scout_152c3a871b5f5e105b0c72a9`;
- conversation `6a6a54a1-7cb8-83e8-b37d-e41190f1254f`;
- the previously bound local browser session;
- `GPT-5.6 Sol Pro`, `xhigh`, and Pro access;
- a complete, non-interrupted response with active browsing; and
- answer hash
  `63dfc3a7922b218928c71598cc422186c666238ad15176b5afa22497619c4825`.

The validated `research_evidence_bundle_v1` has hash
`81bb2d8fec145b314f5cab7a337febba078782278cce01560e4a82b9c2a3d377`.
It contains 16 queries, 23 structured claims, and 16 accepted sources: 13
`TIER_1_OFFICIAL`, two `TIER_2_PRIMARY_DATA`, and one
`TIER_5_SOCIAL`. No claim supported only by the social source was marked
corroborated.

The follow-up resolved the requested release-history question conservatively:

- archived H.4.1 release pages and the published Thursday release rule can bind
  reserve and weekly-average TGA observations to first availability, while a
  current bulk download alone is not revision-safe;
- the searched Daily Treasury Statement interfaces did not prove an immutable
  per-observation publication timestamp or revision-aware TGA vintage, so that
  path remains discovery-only;
- ALFRED exposes vintage retrieval for the ICE BofA high-yield OAS series, but
  ICE rights and redistribution restrictions require a separate data-rights
  review; and
- release-lagged HYG excess return over SGOV is a testable discovery proxy, but
  it is explicitly not equivalent to OAS because it mixes credit spread,
  Treasury duration, carry, and ETF microstructure.

The follow-up Research request
`requested-evidence-research-request_1af8cafbc8cd88f05e66e0f9` is bound to
context manifest
`63ad70888a851062599e18df87558d5fcf760c12b1a98b00173c0254c47eeace`.
It includes the exact IWM-RCG 2.0.0 failure diagnostic and a hash-bound
prospective-readiness snapshot for Q1-DET 2.0.0. That snapshot has payload hash
`81d5f78ef14985fa0352edc46d4e0e383a479e03c6114f762dd8830601158f99`
and records `WAITING_FOR_FORWARD_OUTCOMES`, `0/126`, no falsification input,
no OOS, no shadow, no broker access, and no automatic promotion.

### Isolated IWM-RCG 2.0.1 attempt

Fresh Commander invocation
`commander-6f8abc8fd6d34aa185c1b1a5248c36d3` ran in the separate Commander
repository with `gpt-5.6-sol`, reasoning `max`, ephemeral execution, disabled
resume and user configuration, a successful sibling-read-denial preflight, and
no credential use. Its output hash is
`e9c628b0a61b5f75239968b32ea2e861de56f1035e6e0a6cfaa9273b1acd5377`.
The public Research ledger accepted the resulting
`PROPOSE_STRATEGY_REVISION` decision.

The proposal did not mutate the failed IWM-RCG 2.0.0 tree. It requested a new
IWM-RCG 2.0.1 version over HYG, IWM, QQQ, SGOV, SOXX, and SPY, using only
completed-session, prior-close price features and the existing Candidate
decision ABI. The proposal retained the prior hypothesis but required
tolerance-aware numeric invariants and the full host-owned falsification,
cost, delay, capacity, turnover, drawdown, PIT, and deterministic-replay gates.

Fresh Builder invocation
`builder-bf2ff09433f444e98c7a2ef663334224` ran separately with the same model
family and reasoning profile. Its output hash is
`5d1a9d75244cb0be4dbfe3aeddd54924c2e7e6c60e4e88de0b64b10ca71a90ba`.
It added only the versioned 2.0.1 strategy, configuration, documentation, and
Candidate test files permitted by the proposal. It produced no promotion
decision, broker action, order, or executable Candidate artifact.

The host finalized immutable Challenger
`challenger-9ba46818415847b4b9650cb6` with:

- manifest hash
  `55981805a0498f3abdece1426b263760196d6fbc9efadb8364eca056d5d5d5b8`;
- patch hash
  `881063fd2a9e6216eba32614a8d1c7db6f248d8502b1caaa6eed8ce380071037`;
- code hash
  `8c630b25bf3f8de2535b64505e2294249fd3a22b1bd3fdde3acf2611543be0c5`;
- config hash
  `4852b54e149b4bb112666123e4e29cdadd47c11a0a7fb75b77cf42364d7673fa`;
  and
- test-manifest hash
  `15d5be6aafdf244a2d803ea9cf60a550fd481b00a2b461b36b4eee85ff77a086`.

The authoritative isolated test run collected 33 checks, including the
host-owned ABI check. It passed 31 and failed two. The Candidate source,
Candidate tests, host ABI test, and complete Candidate tree remained
hash-identical before and after execution. Network, credential, broker, and
real-order access were all disabled, and raw process output was not persisted.

An exact disposable diagnostic reproduced both failures. The Candidate's own
32 tests pass in the complete Candidate tree, but the isolated test projection
contains only declared Candidate tests plus changed strategy or research
configuration. Two tests incorrectly attempted to read undeclared files:

- `test_candidate_source_has_no_external_or_privileged_capability` tried to
  read the strategy implementation through the test projection rather than
  importing or inspecting the projected Candidate source; and
- `test_legacy_q1_candidate_files_remain_hash_identical` tried to read the
  unchanged Q1-DET 2.0.0 configuration, which the projection intentionally
  does not copy.

The host patch-policy check already owns changed-path allowlisting and legacy
immutability. This is therefore a test-projection contract failure, not
evidence that the economic hypothesis passed. The terminal failure record hash
is `60feee11612bc1d4a56e9f3643c9fa404b4e75c1533c6c8c2757dc2867b8612c`;
the sanitized diagnostic payload hash is
`c257b8380bdc8cb8de072a06750133205889ba61287fe236a7440ae2cf3a2253`.
IWM-RCG 2.0.1 remains immutable `TEST_FAILED`. It has no Candidate artifact,
falsification result, OOS request, or shadow arm. A repair, if later funded,
must use another version.

At `2026-07-29T20:46Z`, q1 paper run
`paper_q1_research_20260729_v9` remained healthy in `PENDING_BOOTSTRAP` with a
fresh worker heartbeat, no runtime error, and its next calendar-backed
`Q1_SETTLEMENT` cycle at `2026-07-30T13:30:00Z`. Q1-DET 2.0.0 remained
`0/126`; automatic promotion and real broker routing remained disabled.

### Post-close PIT refresh and v10 quote-bundle correction

A pre-session production-path signal check initially rejected the 2026-07-29
QQQ and SOXX daily bars. Both observations had first been captured at
`2026-07-29T18:59:29Z`, before the versioned calendar close at
`2026-07-29T20:00:00Z`. Treating those partial bars as the next session's
completed input would have been look-ahead-unsafe, so the signal failed closed
with `adjusted close cannot be available before its session close`.

At `2026-07-29T21:00:16Z`, a post-close adjusted-history request fetched 1,054
bars and appended 17 provider revisions without replacing any earlier row. A
second identical request at `2026-07-29T21:02:45Z` fetched the same 1,054 bars
and appended zero rows. The production Q1 signal path then accepted:

| Field | Value |
| --- | --- |
| Planned signal cutoff | `2026-07-30T14:00:00Z` |
| Completed-session range | `2026-02-04` through `2026-07-29` |
| Aligned QQQ/SOXX sessions | 121 |
| Source bars | 242 |
| Signal hash | `dbbeab4a721d0fbc4f766bb6f5182ef06ad543651de406b79aa5d60948f81688` |
| Q1 config manifest | `afcaa7ea2939b3ca39ecae9f553794450ca498a0d1a48a19758eaab70479ad36` |
| All records available by cutoff | `true` |

The actual 2026-07-30 calendar is open
`2026-07-30T13:30:00Z` through `2026-07-30T20:00:00Z`. Its generated schedule
contains one bootstrap, one settlement, one 10:00 ET strategic cycle, one noon
LLM review, 25 deterministic NAV/risk checks, 389 regular-session execution
checks, and one close result. No execution cycle is scheduled at or after the
actual close.

The v7 failure record also exposed an independent quote-bundle defect. The
common T0 valuation path placed every inherited HOLD symbol and QQQ into the
same two-second event-time skew bundle. A read-only replay at the exact
79-attempt v7 cutoff reproduced that legacy failure even though the active
QQQ/SOXX pair itself satisfied the configured skew. Inherited positions do not
belong to clean strategy arms, so an unrelated inherited quote must not erase
the evaluation anchor and every benchmark decision.

Public PR
[#36](https://github.com/story7077/adaptive-llm-quant-public/pull/36)
separated the inherited-position valuation set from the active QQQ/SOXX
decision bundle. Every inherited quote still requires PIT validity, a maximum
15-second age, and positive executable bid/ask values. The two-second
cross-symbol skew fence remains unchanged for QQQ/SOXX. If the active SOXX
quote is unavailable or outside the fence, the Q1 arms are explicitly
data-blocked while eligible QQQ benchmarks remain operable; an inherited SOXX
quote cannot bypass that fence. The exact v7 replay passed with the corrected
quote-bundle path.

The PR's local validation returned 692 passes, Ruff success, zero Pyright
errors or warnings, all versioned configurations valid, and a passing full
public-release scan. Both independent GitHub public-release workflows passed.
The PR merged as
`a5c1fd64720c163b58efc126b18dce44a0982083`.

The pre-open v9 rollover audit found zero evaluation anchors, arm states,
decisions, intents, order events, fills, NAV snapshots, ledger transactions,
risk episodes, settlement events, and daily results. Its pending schedule
records remain append-only and readable. The merged code therefore started a
new immutable run, `paper_q1_research_20260730_v10`, rather than changing v9's
code identity.

The v10 deployment is bound to:

| Field | Value |
| --- | --- |
| Source commit | `a5c1fd64720c163b58efc126b18dce44a0982083` |
| Code identity | `workspace:ff4711a887ec5c41661ead78a06b55370e4c22ace96f51c96c9fbe75f0064bd3` |
| Algorithm | `q1_math_core_v1` |
| Q1 config manifest | `afcaa7ea2939b3ca39ecae9f553794450ca498a0d1a48a19758eaab70479ad36` |
| First scheduled cycle | `2026-07-30T13:30:00Z` |
| First strategic cycle | `2026-07-30T14:00:00Z` |

After deployment, v10 reported `PENDING_BOOTSTRAP`, a fresh worker heartbeat,
no runtime error, healthy `GPT-5.6 Sol`/`xhigh` WebGPT readiness, and the same
validated 121-session PIT signal input. All seven supervised local processes
were running once, the status endpoints were loopback-only, automatic
promotion remained disabled, and `real_order_routing=false`.

### v11 Codex Structured Output transport correction

A synthetic, non-account Q1 commander preflight then exercised the actual
selected Codex transport rather than a mocked provider. The first invocation
failed closed before model execution. Pydantic had declared the defaulted
`schema_version` property but omitted it from `required`, while Codex strict
Structured Outputs requires every declared object property to be required.
No policy, portfolio decision, database row, or broker request was created.

Public PR
[#37](https://github.com/story7077/adaptive-llm-quant-public/pull/37)
now derives a transport-only strict schema that requires every fixed property
and removes defaults. The authoritative `Q1LlmOverlayDecision` validation
still runs after transport. A non-zero Codex process exit is also classified
as `TRANSPORT_FAILED`; stderr is neither persisted nor included in the audit
record.

The same isolated synthetic preflight then returned a schema-valid
`GPT-5.6 Sol`/`max` reduce-only response in 46.739 seconds. It selected no
risk reduction, called no broker, wrote no economic database decision, and
kept `real_order_routing=false`. This proves the bounded transport and schema
path, not the quality or profitability of a market decision.

Validation for PR #37 returned 693 passes, one third-party Starlette warning,
Ruff success, zero Pyright errors or warnings, valid versioned
configurations, strict UTF-8 decoding, and a passing full public-release scan.
Both independent GitHub public-release workflows passed. The PR merged as
`bee038c29dc610b202178f348b9584884188cd00`.

Before rollover, v10 still contained zero evaluation anchors, arm states,
decisions, intents, order events, fills, ledger records, NAV snapshots, risk
records, settlements, and daily results. Its pending schedule remains
append-only and readable. The corrected merge therefore started a new
immutable run, `paper_q1_research_20260730_v11`, rather than changing v10's
code identity.

The v11 deployment is bound to:

| Field | Value |
| --- | --- |
| Source commit | `bee038c29dc610b202178f348b9584884188cd00` |
| Code identity | `workspace:91dbc97f85631c8a90d04d22aa3e07de819abc20895be5b342de21b1cd8f51e7` |
| Algorithm | `q1_math_core_v1` |
| Q1 config manifest | `afcaa7ea2939b3ca39ecae9f553794450ca498a0d1a48a19758eaab70479ad36` |
| First scheduled cycle | `2026-07-30T13:30:00Z` |
| First strategic cycle | `2026-07-30T14:00:00Z` |
| Pre-bootstrap replay hash | `2e4e7b864657d3199562680a668ef293bae4508ccad67a1e767774d5810c1d86` |

Two independent pre-bootstrap replays returned the same hash. The mode is
correctly `INCOMPLETE_EVENT_STREAM`: algorithm and routing checks pass while
initial-state and complete-session checks remain false until the actual
calendar session runs. v11 reported `PENDING_BOOTSTRAP`, a fresh heartbeat,
no runtime error, healthy WebGPT readiness, and the selected
`CODEX_SOL_MAX` commander after deployment.

## v12 run-scoped ledger handoff

Public PR
[38](https://github.com/story7077/adaptive-llm-quant-public/pull/38)
closed the last pre-session operations gap: `ledger verify` now accepts every
Q1 arm and can bind its result to one immutable `run_id`. A regression test
proves that a same-named arm in another run cannot alter the scoped result.
The PR was merged as
`f289ea08c65935c1dad0c5b65916d28c09031b1a` after both public security
workflows passed.

The clean replacement run is
`paper_q1_research_20260730_v12`:

| Item | Frozen value |
| --- | --- |
| Source commit | `f289ea08c65935c1dad0c5b65916d28c09031b1a` |
| Code identity | `workspace:1697244db88d65023d6f1f94e7217ce66312c07f203f1260c32fa3a4845b5d71` |
| Algorithm | `q1_math_core_v1` |
| Q1 config manifest | `afcaa7ea2939b3ca39ecae9f553794450ca498a0d1a48a19758eaab70479ad36` |
| First scheduled cycle | `2026-07-30T13:30:00Z` |
| First strategic cycle | `2026-07-30T14:00:00Z` |
| Pre-bootstrap replay hash | `37a889dcfc38d712f0fd000eab7fc8648c1aefb89b17273958e1beb0d1f17ee1` |

Before replacement, v11 had zero rows across all economic Q1 relations.
After replacement, v12 also had zero anchors, state snapshots, decisions,
intents, order events, fills, ledger transactions and postings, NAV snapshots,
risk records, settlements, daily results, and matched results. Two independent
v12 pre-bootstrap replays returned the same hash. Their
`INCOMPLETE_EVENT_STREAM` result is expected: algorithm, routing, hashes,
state machines, and configuration checks pass, while initial-state and
complete-session checks remain false until the actual session.

The deployed v12 status reported `PENDING_BOOTSTRAP`, a fresh heartbeat, no
runtime error, seven Q1 arms, no active LLM policy, disabled Alpaca Paper
canary routing, and `real_order_routing=false`. Its next calendar-derived cycle
is `Q1_SETTLEMENT` at `2026-07-30T13:30:00Z`.

The complete inherited-position quote set and both Q1 risky symbols fit the
free IEX plan exactly at its 30-subscription limit, with no required symbol
missing from the bar or quote streams. The production PIT service resolved a
positive 20-session ADV and 20 source-bar IDs for all nine required symbols.
It also resolved the Q1 signal input to 121 aligned completed sessions and 242
source bars through `2026-07-29`, with a
`2026-07-30T14:00:00Z` cutoff. The stream was connected with a fresh worker
heartbeat and adjusted-history refresh `READY`; stale quotes before the next
regular-session open are expected and cannot execute.

Validation for this handoff:

- `uv run pytest`: **694 passed**, with one third-party Starlette warning;
- `uv run ruff check .`: passed;
- `uv run pyright`: 0 errors, 0 warnings;
- `uv run python -m trading.cli config validate --all`: passed;
- disposable SQLite migration `0021 → 0020 → 0021`: passed;
- public-release secret, provenance, clean-root, and UTF-8 scan: passed; and
- public workflow runs
  [30497533317](https://github.com/story7077/adaptive-llm-quant-public/actions/runs/30497533317)
  and
  [30497547324](https://github.com/story7077/adaptive-llm-quant-public/actions/runs/30497547324):
  passed.

Actual v12 session, run-scoped ledger, and post-close replay evidence remain
intentionally pending until the calendar session executes.

## Candidate test-projection remediation

The preserved `IWM-RCG 2.0.0` and `2.0.1` failures exposed two preventable
Builder-test contract ambiguities rather than evidence that the strategy
hypothesis passed:

- `2.0.0` retained one strict binary floating-point assertion comparing
  `0.19999999999999996` with `0.2`; and
- `2.0.1` passed its complete-tree Candidate tests but two tests depended on
  unchanged source-snapshot paths that the host-owned isolated test projection
  deliberately does not expose.

Both failed Candidates remain immutable terminal records. They were not
modified, deleted, retried, or promoted.

Research Commander public
[PR 12](https://github.com/story7077/adaptive-llm-quant-research-commander/pull/12)
removed the ambiguity for future versions. The isolated pytest projection now
provides `candidate_source_root` for source inspection and
`repository_root` for changed projected configuration. Builder instructions
also require tolerance-aware calculated-float assertions and forbid using
Builder-authored tests to re-prove unchanged Champion files that the host
already hash-checks independently. Candidate source was already the import
projection and remains hash-fenced before and after execution, so this fixture
does not add network, credential, broker, lockbox, or Champion-write access.

PR 12 merged as
`66f4b0b3d096deb70c6c8b85ef3d13a26cf87233` after:

- `uv run pytest`: **142 passed**;
- `uv run ruff check .`: passed;
- `uv run pyright`: 0 errors, 0 warnings;
- clean-root public release scan: `PUBLIC_SAFE`; and
- both public security workflows passed.

The local fresh-process Commander/Builder entry point was then verified to load
that exact clean `main` commit. New Research cycles use the repaired contract;
the historical failures continue to report their original results.

## Completed-session Research trigger and live-PIT handoff

Public
[PR 40](https://github.com/story7077/adaptive-llm-quant-public/pull/40)
added the missing product-owned bridge from one completed paper session into
the existing deep Research Plane. The new append-only
`OPERATOR_DEEP_RESEARCH` work kind records a typed operator trigger and reason,
binds the exact versioned market-calendar session and data cutoff, and cannot
be claimed until the same session's `DAILY_AGGREGATION` work has a terminal
`SUCCEEDED` event. It does not reinterpret or consume the preserved pre-live
backlog. Migration `0022_operator_deep_research_work` and SQLite/PostgreSQL
append-only guards were deployed before the scheduler was restarted. PR 40
merged as `9c0dd74c9025e028ec4bf6501fb4459c33ac056b`.

The first live operator plan is immutable and idempotent:

| Field | Value |
| --- | --- |
| Work kind | `OPERATOR_DEEP_RESEARCH` |
| Calendar session | `2026-07-30`, regular close `2026-07-30T20:00:00Z` |
| Scheduled time | `2026-07-30T22:05:00Z` |
| Data cutoff | `2026-07-30T22:05:00Z` |
| Reason | `FIRST_LIVE_SESSION` |
| Plan hash | `bd57a0e53806d794f936f8b186fc1e6b3f1e58a43a1b38ef8ebdf775d1cfc2e1` |
| Initial state | `PENDING`, attempt 0, no dispatch receipt |

Repeating the same scheduling command returned `created_count=0` and the same
work-item and plan hashes. Database-clock fencing therefore left the plan
pending before its due time. The current Research consumer was restarted after
PR 40 so its in-memory contract includes the new work kind; the trading runtime
and its immutable v12 code identity were not restarted or changed.

The ignored local launch boundary now builds `ResearchRequestV2` from a
point-in-time daily-bar catalog, an immutable Research memory snapshot, and a
persisted Meta Controller action plan. It does not read the template's static
performance, failure, regime, market-evidence, memory, or action-plan
summaries. A preflight at the scheduled cutoff resolved 17 research
instruments, 1,167 to 1,510 completed daily observations per instrument, and
latest completed coverage through `2026-07-29`; the catalog is recomputed after
the session, so these preflight values are not frozen as the final Research
input.

The original `adaptive-cross-asset-alpha-v1` experiment family had consumed its
three-submission budget: one Candidate remained `PROPOSED` and two immutable
versions of the same hypothesis remained `TEST_FAILED`. The budget was not
silently increased. The first prospective live-PIT cycle instead uses the
versioned `adaptive-cross-asset-alpha-live-pit-v1` family with a three-submission
budget, while the earlier Candidates and outcomes remain available through the
trusted recursive memory.

Live preflight independently verified:

- AGBrowse status `ready`;
- headed Chrome and CDP connected;
- the actual ChatGPT UI tuple `GPT-5.6 Sol Pro` / `Pro` / `xhigh`;
- no model or API fallback;
- selected Research Commander `CODEX_SOL_MAX`;
- clean Research Commander commit
  `66f4b0b3d096deb70c6c8b85ef3d13a26cf87233`;
- all seven inherited holding symbols plus QQQ and SOXX present in the IEX
  subscription set; and
- QQQ and SOXX each with 1,509 positive completed daily observations through
  `2026-07-29`, with no point-in-time duplicate conflict.

Populated live status exposed one operations-only serialization defect: the
latest accepted Scout timestamp remained a Python `datetime` inside the
repository status object. Public
[PR 41](https://github.com/story7077/adaptive-llm-quant-public/pull/41)
now converts the CLI boundary to JSON-compatible values and covers the accepted
evidence timestamp in integration tests. It merged as
`91dd693978cb5ea3477612730bca99ab288d203e` after 700 tests, Ruff, Pyright,
all-config validation, a local public-release scan, and both public security
workflows passed.

This handoff proves readiness and immutable scheduling, not a completed
Research Cycle, a profitable strategy, or a successful Challenger. The actual
session, post-close Web Scout, fresh Codex Commander, optional separate
Builder, two deterministic replays, seven run-scoped ledger checks, and final
Research artifacts remain pending. `real_order_routing=false` and
`automatic_promotion_enabled=false` remain enforced.

## Validation

Post-cycle failure-gate validation:

- public repository at merge `fabd3ff2502698cc6a6fcde77e56ccdd1652cffe`:
  **687 passed**, one third-party Starlette/httpx deprecation warning;
- public Ruff, Pyright, all-config validation, and strict UTF-8 decoding:
  passed;
- public PR #32 push and pull-request security workflows: passed;
- Commander repository at merge
  `e1493853d9fcf142d992195971b6e6d345591156`: **141 passed**;
- Commander Ruff, Pyright, strict UTF-8 decoding, and both PR #11 security
  workflows: passed.

Public repository:

- `uv run pytest`: **665 passed**, one third-party
  FastAPI/Starlette deprecation warning;
- `uv run ruff check .`: passed;
- `uv run pyright`: 0 errors, 0 warnings;
- `uv run python -m trading.cli config validate --all`: passed;
- Q1 config manifest:
  `afcaa7ea2939b3ca39ecae9f553794450ca498a0d1a48a19758eaab70479ad36`;
- Research config manifest:
  `2b8475fd62f76d100ea5254847f2492ddbca6fe8d8d60629d394e1bf7e08d203`;
- prospective Candidate config manifest:
  `7a47d85225092cb80072e2222aed9b1c870f1c61ea0c532066d851505d05fff6`.
- prospective outcome config manifest:
  `7ea406b5edf8339d6530654539da36377318d824cc25676e651fbb33865d4ed5`.
- prospective evaluation V2 config manifest:
  `a7406cf9ffe26279c0dddf54624908fb609a120fb6ec12ce8a35f0f7cfb58a5e`.

Post-provenance handoff at merge
`eee2c40818cdcf0931250493261d9c24c0652277`:

- `uv run pytest`: **672 passed**, one third-party Starlette/httpx deprecation
  warning;
- `uv run ruff check .`: passed;
- `uv run pyright`: 0 errors;
- `uv run python -m trading.cli config validate --all`: passed;
- a disposable database upgraded to `0020`, downgraded to `0019`, and
  re-upgraded to `0020`;
- deterministic demo seed, replay, and verification passed with equal replay
  hashes and balanced ledgers; and
- the public-release secret, provenance, and clean-root scan passed.

Current main after PR #25 at
`00c726b9509ea60a3a058d77e8152e65bcff31d6`:

- `uv run pytest`: **673 passed**, one third-party Starlette warning;
- `uv run ruff check .`: passed;
- `uv run pyright`: 0 errors, 0 warnings;
- `uv run python -m trading.cli config validate --all`: passed;
- disposable PostgreSQL upgrade, guarded downgrade/re-upgrade, and authoritative
  database-clock claim proof: passed; and
- main public-release security workflow
  [30434932882](https://github.com/story7077/adaptive-llm-quant-public/actions/runs/30434932882):
  passed.

Commander repository:

- test suite: **137 passed**;
- Ruff: passed;
- Pyright: 0 errors.

A clean disposable SQLite database upgraded to
`0020_candidate_evaluation_dataset_v2`, downgraded to
`0019_candidate_prospective_outcomes_v1`, and re-upgraded to
`0020_candidate_evaluation_dataset_v2`. The CLI downgrade gate accepts
SQLite only, so a localhost PostgreSQL URL cannot be mistaken for the
disposable database.

The synthetic seven-arm demo replay returned
`f76af79eb41d0498769d070352d8928df6f914fe227c9f5b781f5a7829a89b97`
on two independent invocations in the disposable 0020 database. All 11
verification checks passed and every arm ledger balanced.

The preserved v5 and v6 runs remain unmodified and incomplete. The preserved
v7 run reached its first session but never committed the strategic decision
described above. Its two pre-open `replay` invocations returned the same
deterministic incomplete-stream hash,
`2943f560930e333c7ef3a2ff964876e1fe91f161f01cedd1899c3f8296aa83a7`,
with all then-available hash and state-machine checks passing and the required
initial-state/session-completeness checks correctly false. The current v8 run
is also intentionally incomplete until its next actual session. The
repository's complete synthetic Q1 replay tests passed in the current
687-test suite.

### Migration validation incident

The first attempted disposable downgrade used an invalid Windows path
conversion, so the environment override did not apply and the active
PostgreSQL research database was downgraded from 0017 to 0016, then immediately
re-upgraded to 0017. Migration 0017 drops and recreates four chronological
meta-OOS tables. Their current row counts are all zero, but pre-downgrade counts
were not captured, so loss of an unknown pre-existing row cannot be ruled out.
The incident recovery returned the active database to revision 0017 at that
time. The validated SQLite round trip above was then run with an explicit
dialect preflight; subsequent deployment upgrades are performed separately and
never use the downgrade command against PostgreSQL.

## Remaining gates

- continue parent-bound prospective targets and append only outcomes captured
  within each precommitted future-data window;
- assemble predeclared host-owned variants only after 126 valid forward
  sessions and 504 instrument observations;
- run every mandatory falsification and cost/capacity stress;
- request locked OOS only after falsification passes;
- create an independent Challenger shadow arm only after locked OOS passes;
- accumulate the minimum forward period and independent trade count;
- keep promotion manual.

No real broker order was created, routed, or made available during this cycle.
