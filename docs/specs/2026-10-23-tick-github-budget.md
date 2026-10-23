# tick-github-budget

Evidence (planning session measurement, 2026-10-23 08:46-08:49 KST, gh wrapper): 200 GitHub calls in 3 minutes, 197 from one `fa gate tick`. One tick: graphql (board Status) 116, issues/N/events 26, pulls?head= 25, issues/N/dependencies/blocked_by 25, open-issue listing 1. Each tick re-reads every approved issue and rewrites unchanged board cards (every `board.sync_status` call looks up the item and writes). With a 5-minute tick plus kicks this burns the 5,000/hour REST limit.

#138 keeps watchers local and refreshes the gate snapshot every 5 minutes, but its tests count only listing calls. #184 counts only board update mutations. Neither bounds the per-issue reads or the board lookups inside a tick.

Behaviour:
- When nothing changed since the last refresh, a tick makes no board calls and no per-issue reads (events, blocked_by, PR lookup, comments, issue get). The open-issue listing is allowed.
- When one issue changed, only that issue is read again, and its board card costs a bounded number of calls.
- Change detection is a fact comparison (for example the listing's `updated_at` or the label set, against the local record), never a guess. Board Status is written only when it differs from the last value Foragent wrote; the board item id is cached locally.
- A closed blocker still releases its dependents on the next refresh, although the dependents themselves did not change.
- FakeTracker in src/foragent/tracker.py may grow whatever field the real tracker uses (such as updated_at) so the fake behaves like GitHub.

## Measurement (2026-10-23 09:43-10:45 KST, every gh call logged by a wrapper)

3,605 REST calls in one hour, excluding `rate_limit`. By caller: gate ticks about 2,900 (launchd ticks and `python -m foragent.cli gate tick`), the lane view 394 (before it was closed), Claude sessions about 300, inspect notify 132. By endpoint: `pulls?state=open&head=...` 1,176, `issues/N/events` 841, `issues/N/dependencies/blocked_by` 838 — 80 % of all calls, all repeated per issue. With the lane view closed, the gate alone uses about 2,900 calls an hour, so a burst of orchestrator publishes reaches the 5,000 limit. Target: a tick in which nothing changed makes zero calls to these three endpoints.
