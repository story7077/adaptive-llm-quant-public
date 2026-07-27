# Candidate Execution ABI

`candidate_decision_request_v1` is the only input an isolated Challenger may
receive. It contains versioned point-in-time features, current weights, a
host-owned symbol allowlist and portfolio limits. It never contains future
returns, matched-baseline outcomes, fills, PnL, credentials, or broker state.

The Candidate returns `candidate_decision_response_v1` with one score and one
long-only target weight for every allowed symbol. It cannot introduce a symbol,
omit a symbol, relax a cap, select an order, report a fill, or supply its own
performance. The host rejects malformed JSON, binding mismatches, negative
weights, leverage, over-cap weights, non-finite values, timeouts, resource
failures, and non-zero process exits.

## Isolation and attestation

Production evaluation requires `candidate_execution_security_v1`. The
attestation binds the candidate artifact and tree hashes, exact entrypoint,
runtime and worker hashes, isolation implementation and version, and all
resource limits. Its invariant permissions are:

- network access: false
- credential access: false
- broker access: false
- filesystem write access: false
- real order routing: false

An un-attested in-process function is suitable only for unit tests. It is not a
production Candidate executor.

## Trusted artifact registry

The isolated Builder emits `candidate_artifact_bundle_v1`. The trusted host
accepts exactly one immutable bundle per Challenger and validates it against
the persisted `ResearchRequestV1`, accepted `AlgorithmProposalV1`, and
`ChallengerManifestV1`. The bundle binds the source snapshot, Candidate tree,
patch, code, configuration, test manifest, runtime, entrypoint, request
selection, and all invariant-deny permissions.

Registration is append-only under migration
`0013_candidate_artifact_registry`. A deterministic replay is accepted only
when its Candidate artifact, code, and configuration hashes match that
registered bundle. Every mandatory falsification result must carry the same
Candidate artifact, evaluation contract, data manifest, replay, and
deterministic-seed bindings. OOS rejects any mismatch.

## Trusted evaluation

The host runs every versioned decision request twice. The two response-hash
sequences must match exactly. The host, not the Candidate, then joins the
responses to outcomes that became available after each decision and calculates:

- candidate and matched-baseline returns
- one-way turnover, including cash
- commissions, spread and delay costs
- ADV capacity use
- factor and regime evaluation rows
- deterministic replay and evaluation-trace hashes

The same ABI runs inside the OOS lockbox. The Candidate sees only its PIT
feature requests. The lockbox keeps outcomes and row-level results private,
writes an append-only hash-bound dataset, and returns only its manifest and the
predeclared aggregate PASS/FAIL response.

Candidate expiry or model failure cannot create an order. Research output
becomes tradable only after mandatory falsification, deterministic replay, OOS,
independent shadow evaluation, and an explicit manual Champion designation.
