# Portfolio Delta-Sharpe Extension Contract

> **UNIMPLEMENTED — planned follow-up PR 3**
>
> `PredictedPortfolioDeltaSharpeV1` and the outcome-event metric fields are
> storage contracts only. The repository does not currently calculate,
> bootstrap, judge, or optimize portfolio delta Sharpe.

## Intended question

The future trusted judge should answer:

> Does adding this Challenger to the current research portfolio improve
> risk-adjusted performance after matched costs and constraints?

That is different from asking whether the Challenger has a positive standalone
Sharpe ratio. A correlated strategy may add little to the portfolio, while a
modest but diversifying strategy may improve it. The comparison must therefore
use a predeclared portfolio construction and matched common observations.

Conceptually, the point estimate is:

```text
delta_sharpe =
    Sharpe(research portfolio with candidate)
    - Sharpe(reference research portfolio)
```

The exact return aggregation, portfolio weights, rebalancing rule, missing-data
policy, and cost application are not implemented and must be fixed in the
future evaluation contract before this expression is executable.

## Existing carrier fields

PR 1 can record:

- predicted lower, median, and upper portfolio delta Sharpe;
- realized point estimate, lower bound, and upper bound;
- worst-cost lower bound;
- drawdown, tail-loss, turnover, and cost deltas;
- prediction error relative to the stored median prediction.

The ledger validates chronology and arithmetic but does not establish that a
caller used a trusted estimator. Until PR 3 supplies that estimator and binds
its artifacts, manually supplied economic fields are audit data, not promotion
evidence.

## Reserved configuration

| Field | Checked-in value |
| --- | ---: |
| annualization sessions | 252 |
| minimum common sessions | 126 |
| minimum independent trades | 30 |
| bootstrap samples | 5,000 |
| stationary block length | 10 |
| lower quantile | 0.025 |
| variance epsilon | `1e-12` |
| minimum delta-Sharpe lower bound | 0.0 |
| minimum worst-cost lower bound | 0.0 |
| cost stress multipliers | 1×, 2×, 3× |

These are versioned extension parameters. No current service consumes them to
produce a verdict.

## Required future evaluation contract

PR 3 must define and hash-bind:

- the reference portfolio and Candidate-added portfolio;
- common session keys and point-in-time availability;
- identical market, execution, liquidity, and cost scenarios;
- portfolio weights and rebalancing behavior fixed before evaluation;
- minimum common sessions and independent trades;
- annualization and zero-variance behavior;
- deterministic stationary-block bootstrap seed and implementation version;
- 1×/2×/3× cost-stress construction;
- missing, stale, censored, and non-finite observation handling;
- output aggregates and reason codes.

The trusted result should include at least the point estimate, confidence
interval, lower confidence bound, worst-cost lower bound, common-session count,
trade count, data/evaluation hashes, and deterministic replay hash.

## Gate semantics

A future lower-bound threshold is a research gate, not proof of statistical
significance or future profitability. It must not replace:

- mandatory falsification;
- the promotion OOS lockbox;
- independent shadow forward evaluation;
- drawdown, tail, turnover, cost, capacity, and regime checks;
- explicit human promotion approval and Champion designation.

The judge must fail closed on mismatched portfolio composition, timing, costs,
or source manifests. It may issue an evaluation result only; it may not promote
a Challenger or route an order.

## Current non-capabilities

There is no `portfolio_delta_sharpe` implementation, trusted data producer,
bootstrap service, result persistence, scheduler worker, CLI command, or
promotion binding. The PR 1 fields must not be presented as calculated results
unless and until PR 3 implements this contract.
