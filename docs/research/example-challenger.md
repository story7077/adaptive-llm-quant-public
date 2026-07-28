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
