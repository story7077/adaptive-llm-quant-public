# Example Challenger: T1 v1.1.0

This repository includes one synthetic, versioned Challenger to demonstrate the
full rejection path without changing the T1 v1.0.0 Champion.

The proposal replaces the original breadth composite with the equally weighted
fraction of point-in-time members that are above a slow moving average and have
a positive intermediate return. The implementation lives only under
`src/trading/strategies/challengers/`.

The candidate is `REJECTED`. The available synthetic catalog does not provide
complete historical point-in-time constituent membership at the declared
coverage threshold, so the mandatory `pit_constituent_leakage` gate fails.
Because one mandatory test fails, the candidate cannot access the OOS lockbox or
receive a shadow arm. The failed candidate and reason remain append-only.

This rejection is intentional evidence that the pipeline fails closed. It is not
evidence that the economic hypothesis is false; it means the currently available
data cannot test it without leakage.

## First live-built Challenger: Q1-DET v2.0.0

The first actual Web Scout → Commander → Builder cycle produced
`challenger-c0bb5e7ebe50e442a6e39250`, backed by Candidate artifact
`8abe061a438043d6889f7205720c023bae916b508d6d7e3a1b79ba66434cf4c3`.
Its proposal adds a capped GLD/TLT/SGOV diversifying sleeve around the parent
QQQ/SOXX strategy. The isolated Candidate ABI test and all 12 declared
Candidate tests passed.

That build result does not prove the hypothesis. The Challenger remains
`PROPOSED` because it has no mandatory falsification report and no locked OOS
result. It therefore has no independent shadow arm and cannot be promotion
eligible.

The host may record forward-only prospective target-state observations for
this Candidate. Each observation is bound to one completed parent `Q1-DET`
decision, 200 completed PIT-valid sessions for GLD/QQQ/SGOV/SOXX/TLT, the
common evaluation anchor, the sealed Candidate artifact and configuration, and
matching independent primary/replay outputs. These observations mature future
research evidence; they do not create orders, positions, P&L, or a shadow
portfolio.
