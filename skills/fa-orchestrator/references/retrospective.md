# Repeat holds and the retrospective when an order finishes

## Why count by defect class

A reason code (`retries-exceeded`, `review-dispute`, ...) says where a run stopped, not what was
wrong. The same defect class (for example two writers for one state, or check-then-act) can stop a run
under different reason codes, and one reason code can hide different defects. On 2026-10-27, #370 had
taken seven per-site hints (about 15.5 run-hours), #415 four and #154 three: each hint fixed the last
site the reviewer named, and the next review found another site of the same defect. Escalating every
repeat conflicted with D71 (ask the person only for product decisions), so retry-with-hint had become
the default. A second hold for the same defect class therefore means the issue itself is wrong: its
Goal needs redesign (for example one writer per state, D95) or it needs a split, through an order edit
and re-approval.

## Why a retrospective

On 2026-10-27 the #370 loop was found only because a person asked about a merge gap. A short look at
`fa orch facts` and `fa orch log` finds lost run time and repeated failures without waiting for
someone to ask. It runs when an order finishes (D99): a finished order is a unit of work to learn
from, and elapsed time without one gives nothing new to read.

## How it wakes and how it ends

When every child issue of a published order is merged or closed, the gate tick posts one inbox request
with `kind: retrospective` naming the order, and keeps at most one pending. An interactive orchestrator
session holds no machine slot for it. The `scheduled_roles` switch, a plan pause, quota, run hours and
`--no-start` hold it the same way they hold the architect. When no session is open and none can be
opened, the request stays pending; `fa orch facts` shows it, its age and the next retry.

Do these in order:

1. If the person found a problem first, record it: `fa orch note --person-found "<text>"`.
2. Choose the lessons yourself, then append them to `MEMORY.md` in your role's memory directory
   (`memory` under the orchestrator role in `.foragent.yml`). Keep what is there.
3. Run `fa orch note --retrospective --now "<lessons>" --why "<evidence>" --next "<step>"`.
4. Run `fa orch request-status ID done --answer "<what you recorded>"`.

Only step 3 completes the retrospective. The last retrospective time is the time of the newest marked
note in `orch-events.jsonl`; it stays through the 30-day journal trim. A request, a launch or its
closing is not completion: a request closed with no marked note is asked again after one hour.
Ordinary `fa orch note` lines cannot erase a marked note, because each note appends its own event and
only the latest-note file is replaced.

Also read `fa decisions --json`. Rows in state `decided, nothing ordered` are decisions with no
linked issue yet. They are context, not a queue: decide whether to propose an order, defer it or
raise it with the person. Older issues link only through exact D-number citations in their
published spec, so a missing link does not prove a decision was never implemented. `observed_at`
says how old the issue facts behind a row are.

## The architect question

The four signals are read from `fa orch facts` and the log; there is no threshold automation, and
the call stays the orchestrator's judgment (Jin, 2026-10-27):

- overlap waits repeating on the same hot files;
- repeat holds whose defect class is design (two writers, check-then-act);
- files over their line limit;
- approved work concentrated in one lane while other lanes are idle.

Until `fa orch architect` is available, hand code-design causes on as an order.

## Reading the scorecard

The retrospective reads `fa orch scorecard` beside `fa orch facts` and `fa orch log`. A metric worse
than on recent days is a question to answer from the logs, not a target to hit.

- **The same failure reason on two or more issues** (for example two lanes stopped by the same tool
  error, test-slot wait, missing path or quota stop) is an environment problem: the issues are not
  wrong, the place they run is. Find the root cause in the run logs (`log_path`, read beyond the tail),
  then file one fix issue for it as a new order through `fa-order`, in the lane that owns the files.
- **The same defect class twice on one issue** is the hold rule: redesign or split that issue.

## Never buy speed with safeguards

Locked acceptance tests, cross review and the merge conditions are what make a fast scorecard mean
anything. Do not weaken, skip or relax any of them to recover a metric. If one of them looks like the
cause of the slowdown, ask the person first, with choices and a recommendation, and wait for the answer.

## Review notes

Reviewers write root-cause and structure findings as `NOTE:` lines. They never reject the work; the
runner records each one once per issue as a `review-note` event in `orch-events.jsonl`. Read them in
the retrospective and the tidy cycle as evidence for structural work.
