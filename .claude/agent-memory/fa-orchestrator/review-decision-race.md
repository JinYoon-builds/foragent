---
name: review-decision-race
description: answering a review-decision request within seconds of creation can land before the gate's final flag; retry then refuses — re-record the answer
metadata:
  type: project
---

11-09 #2868: I answered r1281 at 20:38:50; the gate re-wrote the review-defects flag at 20:38:55, so `decided_at` <
`flagged_at` and `fa orch retry` kept refusing ("held after two failed rounds").

**Why:** `orch_retry_hold.refusal` releases only on a decision later than the flag.
**How to apply:** if retry refuses after a ship/narrow answer, compare `flags/<n>.json` flagged_at with the request's done
note time; re-run `fa orch request-status ID done --answer "ship with follow-up"` and retry. Follow-up candidate: compare
against the request's creation instead.

**11-09 addendum:** a release also did not narrow the next review (r1288): the orchestrator PASS lacked a head, so the
retry was reviewed whole and the same accepted class held #2824 again after 6.1 h. Ordered 2026-11-09-release-pass. Until
it merges, expect a second hold after a "ship with follow-up" retry; answer it the same way and say the class is accepted.
Also: answer a role-signal "not-now" only after checking the alarm's keys are already in the last review's input —
r1286's #2824 growth was not in r1283.
