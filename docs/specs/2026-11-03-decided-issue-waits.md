# Decided issues waiting for tests, and decision-to-approval time

Jin's decision (2026-11-03): the lane that owns a decided issue drafts its acceptance tests in a
separate session. Item 5 of that decision is measurement. The orchestrator and the architect
need two numbers from the facts and the scorecard they already read:

1. How many decided issues are waiting for tests.
2. How long an issue takes from decision to approval.

## Terms

- **Decided issue waiting for tests**: an open issue with a Done when command that is the
  placeholder. The placeholder is `false` followed by a `# placeholder:` comment, for example
  `false  # placeholder: acceptance tests are written before this issue can be approved`.
  A closed issue does not wait. An issue whose Done when has real commands only does not wait.
- **Decision time**: when the issue was created on GitHub.
- **Approval time**: the approval that the gate recorded for the issue.
- **Decision to approval**: approval time minus decision time, in minutes.

Missing evidence is unknown, never zero:

- Before the gate has recorded any GitHub issue listing, the waiting count is unknown.
  After a listing with no placeholder issue, the count is a known 0.
- An approval whose issue has no known creation time counts as unknown. It is not a time of
  0 minutes, and the median leaves it out. With no known time, the median is unknown.

`fa orch facts` and `fa orch scorecard` still read local records only. They make no GitHub
call. The gate records the creation time when it reads GitHub.

## Reading contract (JSON)

The orchestrator and architect read these names.

`fa orch facts --json`, top level:

- `tests_waiting`: `{"count": <int or null>, "issues": [<issue numbers>]}`
- `decision_to_approval`: `{"approved": <int>, "known": <int>, "unknown": <int>, "median_minutes": <number or null>}`
  for approvals within the facts window (`window_days`).

`fa orch scorecard --json`, in `window.today` and `window.previous`:

- `tests_waiting`: the count now in `today` (null when unknown). The lane chooses what
  `previous` shows.
- `decision_to_approval_minutes`: in `today`, the median for approvals in the last 24 hours;
  in `previous`, the daily median of the days before, by the scorecard's usual rule.
  Null when no time is known.
- `decision_unknown`: approvals in that window with no known creation time.

Both human views show the waiting count and the decision-to-approval median, and say how
many approvals are unknown. The lane chooses the wording, placement and any other fields.

## Out of scope

- Starting or scheduling the lanes' test-drafting sessions; the red-on-main check (order lane).
- Alerts, role signals or thresholds on these numbers.
- Changes to the fa-orchestrator or fa-architect skill text.
- Back-filling creation times for issues that the gate no longer lists.
