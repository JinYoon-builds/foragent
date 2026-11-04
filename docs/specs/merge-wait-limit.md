# Let merge-ready runs wait through today's measured tail

## Problem and cause

On 2026-11-01 the one-hour cumulative merge-wait ceiling cut #1062, #993 and #1191 shortly before their merges. Each became blocked and needed a person. The supplied 36-hour measurement has 81 issues: median 135 minutes, p90 474 minutes, maximum 826 minutes (#1232), including the 2026-10-31 night incident. These measurements are supplied task evidence, not a new measurement made by this draft.

At verified base 232cd9557c8cfba0abc00d16067cd5e0af1966ba, src/foragent/run_merge_turn.py defines MERGE_WAIT_SECONDS = 60 * 60. Both await_turn and refused-merge retry pauses spend Run.merge_waited_seconds. await_turn checks only local queue liveness and the cumulative ceiling; it does not refresh terminal GitHub issue/PR facts. _report_wait_limit withdraws the row and creates an inbox request but emits no distinct hit event. The problem is missing remote facts and a ceiling below the observed normal tail, not an agent's semantic judgment. This order changes those factual/accounting boundaries and adds no meaning classifier.

GitHub's connected read of main confirmed this base on 2026-11-01, matching the locally advanced origin/main. The working tree HEAD remains 3ec75d821c6e5c68658182e8769208b29cda2b95 and is untouched. Main advanced during drafting; final checks use a clean isolated checkout of the newer base. The shell's git ls-remote failed DNS, so the connected GitHub read supplied the remote verification.

## Required outcome

A ready run may wait for its merge turn for up to 16 hours (57,600 seconds), cumulative across queue waits and refused-merge retry pauses in that run, while actual implementation/check/review work keeps its existing budget. A wait longer than one hour and the measured 826-minute maximum can finish normally once its turn arrives. The 16-hour value comes from the maximum measured on 2026-11-01, including the 2026-10-31 night incident. Re-measure and lower this temporary ceiling when the flow records exist; 16 hours is not a permanent target.

Inside the existing merge-turn wait loop, read whether this issue has closed or its already-published PR has merged or closed every five minutes. On a confirmed terminal fact, end this run promptly, release its file claims and withdraw its queue row, without attempting a merge, reopening a PR/issue, or asking a person. An absent open-PR match is usable only for this run's known published PR, not as a guess that an arbitrary missing reference has ended. The first check may be immediate or at five minutes; an uninterrupted healthy wait then checks every 300 elapsed seconds. A round may read the issue and the PR separately; neither fact family may be read more than once in that interval for this run, including after a failed read or re-entry into waiting. A terminal change just after a check is detected by the next due check.

An ordinary GitHub read failure means unknown: keep the run waiting and retry at the next scheduled check. Never treat a transport failure as a closed issue/PR. Preserve the existing GitHub rate-limit pause/resume behavior rather than swallowing rate limits as ordinary failures. Waiting still credits elapsed time, keeps liveness visible and leaves actual work budgets intact. The probes are read-only factual checks in this loop: no new persistent state, storage path, worker, background task or agent judgment rule.

Every real cumulative limit hit retains today's inbox request and adds exactly one countable local observation in the existing event journal. The public orch_events.events(store, "merge-wait-limit") reader exposes one observation per hit with issue, run_started_at, at, waited_seconds and limit_seconds. This is the minimal measurement contract; private helper names and implementation structure belong to the lane. The observation survives StateStore reload, distinguishes executions, and final cleanup does not add a second hit. Keep the PR and branch, withdraw the queue place, release claims, and retain the current merge-wait outcome, blocked labels, elapsed-wait/poll details and inbox handoff. Normal completion, remote termination and GitHub availability waits produce no limit-hit record.

## Scope and ownership

One issue, size m, implicit main lane. No .foragent.yml lane paths match src/foragent/run_merge_turn.py; main is the existing fallback. Expected implementation paths are src/foragent/run_merge_turn.py and focused unit coverage in tests/test_run_merge_turn.py. The locked acceptance targets are tests/acceptance/test_merge_wait_limit.py and the supplied replacement of tests/acceptance/test_merge_wait_budget.py. Existing public runner cleanup and GitHub wait facilities remain available for reuse. No dependency on another draft or future helper is required.

Changing the merge worker/queue architecture, ordering, CI polling or policy, implementation/review timeout, generic GitHub availability handling, scheduler decisions, agent instructions or semantic judgment; building or depending on the separate flow-records draft; adding durable probe state or a new storage/worker path; publishing this draft or implementing source changes during this drafting task. The only existing locked acceptance replacement authorized by this order is the supplied test_merge_wait_budget.py content, with all original node IDs and active-work timeout controls retained. Any further discovered locked-test conflict needs replacement content retaining its IDs in this order before implementation.

## Acceptance and replacement

The new file drives the public run_issue path with real queue, wait accounting, claims, checks, cleanup and publication, using offline Git/executor/tracker/time boundaries. It locks remote issue closure, PR merger, unmerged PR closure, transient failure on either fact read, deterministic five-minute read counts across a refused merge and re-entry, ordinary merge after waiting, 403/429 availability waits, and a 57,600-second cumulative limit hit with one journal record plus the unchanged inbox request. It asserts effects: held claims before termination, released claims and explicit withdrawal afterward, no merge attempt on termination, and a real published feature head on success. It does not import new production symbols or any other draft.

The existing test_merge_wait_budget.py does not contain a 3,600-second assertion at this base: its waits are only 90 and 185 seconds. Its replacement extends those two existing parameter cases to 3,900 seconds (65 minutes) and 49,560 seconds (826 minutes), keeping IDs long-wait and longer-wait. It removes only the fixture's old 100-poll ceiling, replacing it with a finite workload-scaled guard. Both actual-work timeout controls and every node ID remain intact. These strengthened cases fail against today's one-hour limit. Inspection found no other locked merge-wait test that demands a 3,600-second ceiling. The unit tests using patched 30/60/90-second limits test accounting, not today's default; their identities stay available to the implementer.

The draft carries complete replacement bytes in acceptance_files. Publication of this order must apply these bytes together with the new file; leaving the replacement only in prose would leave obsolete acceptance coverage.

## Verification

Run only the explicit targets below, never the full suite:

```sh
uv run pytest -q tests/acceptance/test_merge_wait_limit.py tests/acceptance/test_merge_wait_budget.py
uv run pytest -q tests/acceptance/test_merge_turn.py tests/acceptance/test_merge_turn_priority.py tests/acceptance/test_github_limit_wait.py
uv run pytest -q tests/test_run_merge_turn.py
```

Final baseline checks cover every existing locked file in Done when, including the original test_merge_wait_budget.py before applying its replacement, plus tests/test_run_merge_turn.py. The final observed result is recorded in verification.md. The new file must collect in an isolated clean checkout of the stated base with only this issue's acceptance_files overlaid. Collection is not a substitute for the expected baseline behavioral failures. Final observed counts and exact commands are retained in this draft's verification.md. No post-implementation pass is claimed by this source-unchanged draft.

## Assumptions

The first terminal-fact check may run immediately or after five minutes. The five-minute bound is per issue/PR fact family, allowing the two necessary facts in one round; failed attempts spend the interval. No exact new normal-stop outcome string is prescribed. The event name and minimal fields above are a public measurement contract so hits can be counted without parsing prose; they do not introduce a new journal or depend on flow records. The 16-hour ceiling is provisional and must be revisited after flow records exist. This is a local unpublished draft; source remains unchanged.
