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
`q1_math_core_v1` paper runtime is live with a synthetic account and is waiting
for the next observed market session; it does not run this unvalidated
Challenger.

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

- mandatory falsification reports: 0;
- locked OOS results: 0;
- Challenger shadow registrations: 0;
- promotion-eligible Challengers: 0.

The next valid step is prospective PIT collection followed by the declared
falsification suite. A mandatory failure must reject the Candidate before OOS
or shadow. At least 126 common out-of-sample sessions are required before any
manual promotion recommendation.

## Operational Q1 paper runtime

The active synthetic run is `paper_q1_research_20260729_v4`.

At `2026-07-28T18:25Z` its server-reported state was:

- algorithm: `q1_math_core_v1`;
- Alpaca data feed: IEX, `CONNECTED`, `LIVE`;
- coverage: single exchange, not SIP/NBBO;
- paper quote input: ready;
- run state: `PENDING_BOOTSTRAP`;
- evaluation anchor: not yet established;
- next cycle: `Q1_SETTLEMENT` at `2026-07-29T13:30Z`
  (09:30 ET);
- LLM operational overlay: `NO_POLICY`;
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

## Validation

Public repository:

- `uv run pytest`: **614 passed**, one third-party
  FastAPI/Starlette deprecation warning;
- `uv run ruff check .`: passed;
- `uv run pyright`: 0 errors, 0 warnings;
- `uv run python -m trading.cli config validate --all`: passed;
- Q1 config manifest:
  `afcaa7ea2939b3ca39ecae9f553794450ca498a0d1a48a19758eaab70479ad36`;
- Research config manifest:
  `e7f2d7bb6876431f8b1e17508ed4f35baf69548b62d877f73d771a9b9b6b2a5b`.

Commander repository:

- test suite: **137 passed**;
- Ruff: passed;
- Pyright: 0 errors.

A clean disposable SQLite database upgraded to
`0017_chronological_meta_oos_v1`, downgraded to
`0016_portfolio_delta_sharpe_v2`, and re-upgraded to
`0017_chronological_meta_oos_v1`.

The live v4 run is not yet replay-complete because its first session has not
opened. `replay` and `verify` returned the same deterministic incomplete-stream
hash,
`d11e7915554fe798d68bc9fbdf178aec5420df305736c820959afed69f2fbede`,
with all available hash and state-machine checks passing and the required
initial-state/session-completeness checks correctly false. The repository's
complete synthetic Q1 replay tests passed in the 614-test suite.

### Migration validation incident

The first attempted disposable downgrade used an invalid Windows path
conversion, so the environment override did not apply and the active
PostgreSQL research database was downgraded from 0017 to 0016, then immediately
re-upgraded to 0017. Migration 0017 drops and recreates four chronological
meta-OOS tables. Their current row counts are all zero, but pre-downgrade counts
were not captured, so loss of an unknown pre-existing row cannot be ruled out.
The active database is back at revision 0017. The validated SQLite round trip
above was then run with an explicit dialect preflight.

## Remaining gates

- collect prospective PIT-adjusted bars and revision provenance;
- run every mandatory falsification and cost/capacity stress;
- request locked OOS only after falsification passes;
- create an independent Challenger shadow arm only after locked OOS passes;
- accumulate the minimum forward period and independent trade count;
- keep promotion manual.

No real broker order was created, routed, or made available during this cycle.
