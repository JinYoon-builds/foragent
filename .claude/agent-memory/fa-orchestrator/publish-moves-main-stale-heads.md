---
name: publish-moves-main-stale-heads
description: Each order publish adds locked tests to main; runs whose branch predates it fail CI as "lost passing tests" until heads can be synced
metadata:
  type: project
---

On 2026-11-04 I published five orders in about two hours while 7-10 runs were in flight. #1475 and #1793 (both review PASS) then failed CI with "Acceptance comparison lost 3 passing test(s)". Their branch lacked locked tests that reached main after it branched, and the runner merges main into a head only when the issue's own locks differ (runner.py:206).

**Why:** this is the stale-shard / stale-branch defect held for the merge owner on 11-03. Frequent main moves turn it from rare into routine.

**How to apply:** until heads are synced before CI (merge owner or a sync command), batch order publishes into one commit and publish between waves, not during them. When an issue fails CI on lost tests that it never touched, park it with this reason instead of retrying with a hint (the implementer cannot fix it), and tell the PM. Related: [[merge-owner-before-launch]], [[no-chained-publish]].

Which tests cause it: only new locked cases that already PASS on main (guard and edge cases, e.g. "502 does not ask for the scope"). Cases that fail on main are not counted as lost. So an order whose new file has guard cases passing on main is the risky kind; an edit with no new acceptance files is safe to publish mid-wave.
