# Watchers read local records; GitHub is touched once per 5 minutes

Decision (Jin, 2026-10-22, after the GitHub REST limit ran out at 22:41 KST):
- Watchers (lane view, `fa orch watch`, `/fa` briefing) never call GitHub; they read local records only.
- Every change Foragent itself makes on GitHub (labels, PR, merge, comments, close) is written to local
  records at the moment it is made.
- Only what people do directly on GitHub is picked up by the gate, once per 5 minutes.
- Only when a person asks "how is it now?" does one command (`fa gate status --live`) read GitHub immediately.
- Goal: the lane view open all day makes 0 GitHub requests.
- Until this lands, the orchestrator watches with `fa orch watch --interval 300`.

## Evidence (measured 2026-10-22 22:50 KST, gh api rate_limit before/after each command)
| Command | REST | GraphQL |
| --- | --- | --- |
| `fa gate status --json` | 0 | 44 |
| `fa gate watch --once` (one lane-view refresh) | 0 | 31 |
| `fa inspect --json` | 0 | 6 |
| `fa orch list --json` | 0 | 2 |
- The lane view refreshes every 5 s: 31 x 720 = ~22,000 GraphQL requests per hour per open view.
- REST: 767 used in the 9 minutes after the 22:42 reset (~5,100/hour) while 5 runners worked; the REST limit
  (5,000/hour) ran out at 22:41. The measured watcher commands used no REST, so the REST burn comes from the
  gate and runners (labels, timelines, PR checks); one gate tick could not be measured (tick lock held).
- Uncertain: GraphQL totals from `rate_limit` did not add up to the per-command deltas (likely point-based
  accounting). Re-measure after the fix with the same method.

## Order of work
These two issues go to the front of the orch and view lanes: queued issues in those lanes are made to wait
for them (GitHub blocked-by links; approval contents unchanged). `fa brief` (#113) must use the same local
snapshot.
