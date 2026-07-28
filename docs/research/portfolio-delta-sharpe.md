# Portfolio-level Delta-Sharpe V2

> **Implemented, disabled for automatic operation**
>
> The trusted host can construct and persist the contracts and evaluate synthetic
> or private-worker evidence. Recursive scheduling and automatic promotion remain
> disabled. No component can route a real order.

## Quantity being estimated

The estimator answers one whole-portfolio question:

```text
DeltaSharpe
= Sharpe(candidate full portfolio)
- Sharpe(current Champion full portfolio)
```

It never substitutes standalone Candidate Sharpe, Sharpe of a return-difference
series, or a mean-return difference. A new Candidate sleeve is added at its
predeclared risk budget. A revision replaces only the declared Champion sleeve.
Both resulting full portfolios use the same starting NAV, sessions, market
inputs, execution contract, cost model, and risk-free series.

For daily net return `r` and common daily risk-free return `rf`:

```text
excess = r - rf
Sharpe = sqrt(A) * mean(excess) / sample_std(excess, ddof=1)
```

`A` is the contract's annualization count. Variance at or below the configured
epsilon, fewer than two observations, non-finite values, duplicate sessions,
non-point-in-time rows, and returns outside the configured absolute bound fail
closed.

## Immutable comparison contract

`PortfolioComparisonContractV1` is created and stored before any OOS reservation.
It binds:

- Champion and Candidate portfolio manifests and Candidate artifact;
- `ADD_SLEEVE` or `REPLACE_SLEEVE`, the affected sleeve, risk budget, allocation
  policy version/hash, and the policy's data cutoff and creation time;
- starting NAV, market-data, execution, cost, and risk-free manifests;
- strict common-session intersection without interpolation;
- annualization, stationary-bootstrap, cost-stress, and return-validity rules.

The database permits one immutable contract per Challenger artifact. A second
contract, an UPDATE/DELETE, or creation after an OOS reservation/result is
rejected. Migration `0016_portfolio_delta_sharpe_v2` installs SQLite and
PostgreSQL append-only guards.

## Paired stationary bootstrap

One stationary-bootstrap index stream is sampled and applied to Candidate and
Champion rows together. Each sample computes the two portfolio Sharpes
separately and subtracts them. The result records the point estimate and the
configured lower/upper percentiles.

The deterministic seed binds:

```text
configured_bootstrap_seed
candidate_artifact_hash
evaluation_contract_hash
portfolio_comparison_contract_hash
```

The same rows, artifact, and versioned contracts therefore reproduce the same
metrics and result hash. Bootstrap samples themselves are never returned.

## Cost stress

For 1x, 2x, and 3x costs, the trusted estimator applies each portfolio's own
turnover cost to that portfolio, recomputes each Sharpe, and then computes
DeltaSharpe and its paired lower bound. The worst-cost lower bound is the
minimum of the three conditions; costs are not approximated by subtracting one
turnover-difference penalty.

## OOS V2 boundary

`OosWorkerRequestV2`, `OosWorkerResponseV2`,
`PrivateOosDatasetManifestV2`, and `OosLockboxResultV2` coexist with the
unchanged V1 protocol. The host sends only IDs, hashes, thresholds, and the
pre-OOS comparison contract. The fresh worker reads its configured private
root, verifies every artifact/manifest/PIT binding, and returns only:

- PASS/FAIL and bounded reason codes;
- common-session and independent-trade counts;
- Candidate and Champion full-portfolio Sharpe;
- DeltaSharpe point/LCB/UCB;
- aggregate 1x/2x/3x cost results and hashes.

Daily or trade returns, dates, session keys, observations, positions, and raw
bootstrap samples are forbidden in the response and are never exposed to the
Commander or Builder.

V2 passes only if every condition is true:

```text
common sessions >= minimum
independent trades >= minimum
DeltaSharpe LCB > configured threshold
worst-cost DeltaSharpe LCB >= configured threshold
variance is non-degenerate
portfolio and allocation bindings are valid
all metrics are finite
```

The trusted V2 producer runs the Candidate on hidden point-in-time inputs and
integrates its output using the already-fixed allocation policy. It cannot
optimize the Candidate weight from OOS results.

## Shadow and promotion V2

`TrustedShadowPerformanceSummaryV2` applies the same paired estimator to
matched forward evidence. Raw daily evidence stays in the trusted summarizer.

`PromotionEvidenceV2` and `TrustedPromotionEvaluationV2` require OOS and shadow
to pass their DeltaSharpe lower-bound gates independently. They also require
the worst-cost lower bound, portfolio-contract binding, allocation chronology,
and all existing drawdown, tail-loss, turnover, capacity, regime, runtime,
replay, and mandatory-falsification gates.

An eligible result still says `ELIGIBLE_REQUIRES_MANUAL_APPROVAL`. Automatic
promotion remains unavailable; a separate explicit human approval and
expected-version-fenced Champion designation are required.

## Versioned defaults

| Parameter | Checked-in value |
| --- | ---: |
| annualization sessions | 252 |
| minimum common sessions | 126 |
| minimum independent trades | 30 |
| bootstrap samples | 5,000 |
| expected stationary block | 10 sessions |
| lower percentile | 0.025 |
| variance epsilon | `1e-12` |
| configured seed | 7077 |
| minimum DeltaSharpe LCB | 0.0, strict `>` |
| minimum worst-cost LCB | 0.0, `>=` |
| cost multipliers | 1x, 2x, 3x |
| maximum absolute daily return | 1.0 |

The authoritative JSON schemas and their byte hashes are under
`contracts/portfolio-sharpe-v2/`.

## Failure and recovery

- A missing/mismatched manifest, late row, invalid metric, degenerate variance,
  malformed worker output, timeout, or hash conflict fails closed.
- Never copy a private OOS dataset into a Candidate worktree or public artifact.
- Retry persistence only with the identical immutable request and idempotency
  identity.
- A changed allocation, Candidate, code, data, threshold, or contract is a new
  evaluation and consumes the applicable experiment budget.
- No lower bound proves profitability or statistical significance; it is one
  conservative research gate.
