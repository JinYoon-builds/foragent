# Merging work done outside the gate

Work done outside the gate (an issue implemented by hand) skips the gate's acceptance-regression
check. Before merging it, check what the gate checks: no acceptance test passing on the base fails
on the PR head (`fa orch premerge PR --json` compares both), the unit suite passes and CI is green;
locked tests of unbuilt issues fail on both sides and do not count (on 2026-10-23 an outside merge
broke eight acceptance tests on main). While it is worked outside, remove `approved` (and
`blocked`/`needs-human`) so the gate does not start it; reinstall Foragent after an outside merge.
