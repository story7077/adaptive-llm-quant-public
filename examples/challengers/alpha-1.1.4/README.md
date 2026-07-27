# Alpha 1.1.4 Challenger example

This directory is a sanitized, synthetic record of one real end-to-end
Research Plane cycle. The Candidate Builder produced a versioned Challenger
against the public `CandidateDecisionRequestV1` / `CandidateDecisionResponseV1`
ABI. It did not modify the Champion.

## Outcome

- Challenger: `challenger-b47059232851e41cbd58fc48`
- Commander: `CODEX_SOL_MAX`
- Builder: `gpt-5.6-sol`, reasoning profile `max`
- Candidate unit and host ABI tests: 19 passed
- Deterministic replay: passed with matching hashes
- Mandatory falsification: failed
- Failed test: `single_symbol_or_month_dependence`
- Reason: `SINGLE_SYMBOL_OR_MONTH_DEPENDENCE_DETECTED`
- OOS and shadow admission: blocked
- Automatic promotion: unavailable
- Real broker routing: unavailable

The strict host-owned evaluation used 252 synthetic sessions, 1,512 scenarios,
and 3,024 observations. The two-symbol example was too concentrated to pass the
mandatory dependence test. The system preserved the failed Candidate and did
not tune it after observing the result.

## Files

- `algorithm-proposal.json` — the approved structured proposal.
- `candidate-build-result.json` — the Builder's structured result.
- `candidate-manifest.json` — immutable Challenger identity and hashes.
- `candidate-artifact-bundle.json` — ABI and runtime capability declaration.
- `candidate-test-attempt.json` — passing host-owned test attempt. The earlier
  infrastructure failure remains in the private append-only run record.
- `validation-request.json` — mandatory validation contract.
- `strict-validation-summary.json` — replay, falsification, and OOS-gate result.
- `patch.diff` — generated source and test patch. It is retained as an
  immutable research artifact and is not applied to the Champion.

All symbols (`EXPA`, `EXPB`), market observations, and account assumptions in
this example are synthetic. No credentials, account identifiers, real account
values, browser state, or raw proprietary source payloads are included.
