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

Correction (2026-11-04 19:30Z): the merge owner did NOT remove this risk. I published #1825 (8 guard cases passing on main) assuming the owner refreshes heads before CI; #1816's run then failed CI with 'lost 8 passing tests'. The run's CI still compares its own head. Keep holding orders whose new cases pass on main until the head sync before CI is fixed (shard/stale work with the PM), or publish them only when few runs are in flight.

Under the merge owner (2026-11-04 19:55Z): every main move makes the owner refresh each queued PR and rerun CI (~17 min with the slow shard). I moved main 9 times in 90 minutes with separate order publishes, and the owner merged only one PR. Rule: while PRs wait in the owner queue, publish at most one order commit per merge, batching all ready plans into one publish.

Also moving main (2026-11-04): editing these memory files auto-commits "Foragent role memory" to main within minutes, and uncommitted lane MEMORY.new.md lines in the shared checkout make `fa order publish` fail on pull ("local changes would be overwritten"). Batch memory edits right after a publish or merge; before publishing run `git pull --rebase --autostash origin main` in the shared checkout.
