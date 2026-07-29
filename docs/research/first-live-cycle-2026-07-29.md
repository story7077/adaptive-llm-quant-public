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
