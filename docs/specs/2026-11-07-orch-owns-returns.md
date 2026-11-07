# The orchestrator owns stale locked tests and returned publications

## Problem
On the night of 2026-11-06, 10 of 20 approved issues sat blocked and lanes ran 5 of 15 slots.
Four of five spec questions (#2176, #2340, #2352, #2161) were stale locked-test expectations:
merged main had added test cases or changed a pass count, while the test's purpose was unchanged.
The orchestrator forwarded them to the PM because its skill limits test ownership to tests it
"owns or wrote". Order publications returned to preparation waited 2 to 13 hours: the
orchestrator inbox request was opened, then left "processing" (waiting for a PM answer on the
red-on-main rule for unchanged regression commands) or marked done with "republish later" while
the publication stayed in preparation. Edit-only publications have no prepared issues to fall
back to. Separately, the UX researcher ran one session per finished order; after 11 orders
finished at 05:17 it ran every 7 minutes (115 calls that day).

## Decision (Jin, 2026-11-07)
- Stale locked-test expectations (purpose unchanged; counts, pass counts, or behaviour that
  merged main changed) are corrected by the orchestrator through an order edit and re-approval.
  Only product-behaviour questions go to the PM.
- A publication returned to preparation is the orchestrator's work until it is published again
  or withdrawn; the flow never waits on a closed PM session.
- The publication check accepts a lock-only correction proved on the issue branch it unblocks (q46/q53).
- Regression-only Done when commands that pass on main are dropped from edits; PR CI guards regressions (q51). This is an orchestrator rule, not a check rule.
- The UX researcher follows all orders that finished together in one session.

## Out of scope
- Changing what the publication check or the reviewer requires from new tests.
- Other role schedules (architect, docs, retrospective).
