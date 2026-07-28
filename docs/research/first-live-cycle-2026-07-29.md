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
- 200 aligned completed sessions for GLD, QQQ, SGOV, SOXX, and TLT;
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

## Operational Q1 paper runtime

The active synthetic run is `paper_q1_research_20260729_v5`.

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

## Validation

Public repository:

- `uv run pytest`: **639 passed**, one third-party
  FastAPI/Starlette deprecation warning;
- `uv run ruff check .`: passed;
- `uv run pyright`: 0 errors, 0 warnings;
- `uv run python -m trading.cli config validate --all`: passed;
- Q1 config manifest:
  `afcaa7ea2939b3ca39ecae9f553794450ca498a0d1a48a19758eaab70479ad36`;
- Research config manifest:
  `2b8475fd62f76d100ea5254847f2492ddbca6fe8d8d60629d394e1bf7e08d203`;
- prospective Candidate config manifest:
  `8c3bda4b64c55d350448821c0f14d91f24da4656b9aeeaa1368656ef9e069fa0`.

Commander repository:

- test suite: **137 passed**;
- Ruff: passed;
- Pyright: 0 errors.

A clean disposable SQLite database upgraded to
`0018_candidate_prospective_v1`, downgraded to
`0017_chronological_meta_oos_v1`, and re-upgraded to
`0018_candidate_prospective_v1`. The CLI downgrade gate now accepts SQLite
only, so a localhost PostgreSQL URL cannot be mistaken for the disposable
database.

The synthetic seven-arm demo replay returned
`ab7ca27aab4152b0cea1951a39c4b3bc552d4727ef399a64a03f1d53efbf096c`
on two independent invocations. All 11 verification checks passed and every
arm ledger balanced.

The live v5 run is not yet replay-complete because its first session has not
opened. `replay` and `verify` returned the same deterministic incomplete-stream
hash,
`1af290f96e41284c6e4ce70081bba141be00358757b10a2552371354c633cbd0`,
with all available hash and state-machine checks passing and the required
initial-state/session-completeness checks correctly false. The repository's
complete synthetic Q1 replay tests passed in the 639-test suite.

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

- record the first parent-bound prospective target and continue chronological
  forward observation without backfill;
- run every mandatory falsification and cost/capacity stress;
- request locked OOS only after falsification passes;
- create an independent Challenger shadow arm only after locked OOS passes;
- accumulate the minimum forward period and independent trade count;
- keep promotion manual.

No real broker order was created, routed, or made available during this cycle.
