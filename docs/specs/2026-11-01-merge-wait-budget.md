# Merge-turn waiting preserves the implementation budget

Date: 2026-11-01. This is one choice-free fix, size s.

## Problem and evidence

Runs #1233 and #1168 had finished implementation, review and checks, then waited
for their merge turn in phase `merge-lock-wait`. The reported waits began on
2026-10-31 at 10:06Z and 10:19Z respectively. They ended blocked with reason
`timeout` and “Too big for one session; split it in order”, requiring a retry.
These incident facts come from the supplied task; no network lookup was made.

On main d8c210991281045990bd08373528c41fb4419fc2,
`src/foragent/run_merge_turn.py:await_turn` checks `runner._remaining` and sleeps
without accounting for that time as waiting. `src/foragent/run_waits.py:_remaining`
subtracts recorded waits from elapsed wall time; `_timeout` always flags reason
`timeout` with “Too big for one session; split it in order”. The publication
path checks the same remaining budget again after the queue releases, including
CI and final merge validation. This is a mismatch between waiting and work time,
not evidence that an already completed change is too large. Other waits already
have distinct accounting. Renaming the queue timeout alone leaves the defect.

## Required result

A ready run can wait longer than its remaining implementation time and still
merge when its turn arrives. Merge-turn waiting does not spend implementation
time or replenish time already spent on work. Actual implementation, review,
checks and other active work retain their existing budget behavior.

A run that stops only because it waited for its merge turn is never described as
too big or told to split the order. If merge turns have a wait limit, that limit
is independent of the implementation budget. Its expiry says that the run waited
for its merge turn and how long, preserves the PR, and rejoins the queue or
reports the wait to the orchestrator for recovery. Whether to introduce such a
limit and its implementation remain with the lane; this order requires no new
setting, timeout value or internal representation.

The approved PR merges exactly once after the preceding live run releases the
turn. Existing checks, review, approval and merge-turn ordering still apply.

## Scope and ownership

Use lane `main`: `.foragent.yml` has no lane path matching
`src/foragent/run_merge_turn.py`. The orch lane claims `runner.py` explicitly,
not all run modules. No lane definition change is part of this order.

Out of scope: changing the implementation timeout policy or message for actual
work overruns, raising the global runtime, changing priority or FIFO ordering,
changing review/check or merge-group policy, and operational retries of #1233
or #1168. No script interpretation or new agent judgment rule is requested.

## Locked acceptance

Publish the supplied file at `tests/acceptance/test_merge_wait_budget.py`; the
implementer cannot edit the locked file. It calls the public `run_issue` entry
point and public queue/state APIs. The existing shared runner fixture provides
real checks and isolated Git history with offline Git transport; executors,
tracker and time are fake. No budget, timeout or queue-wait internals are patched.

- After 40 seconds of work in a 60-second run, a live predecessor keeps the run
  waiting for at least 90 or 185 seconds. The run then merges its published head
  exactly once, closes the issue, and has no timeout flag or split instruction.
- A 61-second implementation still ends with the existing timeout behavior.
- After the same 40 seconds of work and a long queue wait, 21 more seconds of
  actual PR checking exhaust the remaining 20 seconds. The PR stays available.

The final command from the repository root is:

```sh
uv run pytest -q tests/acceptance/test_merge_wait_budget.py
```

The draft lives only under `order-out/`. For this pre-implementation check the
same source is run there, with the existing test support explicitly loaded:

```sh
UV_PROJECT_ENVIRONMENT=/Users/apple/Dev/foragent/.venv UV_NO_SYNC=1 UV_OFFLINE=1 UV_NO_CACHE=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD/src:$PWD/tests" TMPDIR="$PWD/.verification-tmp" FORAGENT_UPDATE_CHECK=off FORAGENT_VIEW=off FORAGENT_ORCH_SPAWN=off uv run pytest -q -p no:cacheprovider -p conftest --basetemp="$PWD/.verification-tmp/pytest" order-out/tests/acceptance/test_merge_wait_budget.py
```

## Break-it review

Wrong result: returning a constant merged outcome fails the persisted outcome,
issue state, flag, published-content and merge-effect assertions. Skipped effect:
no merge fails the exact merge-call and merged-PR assertions. Doubled effect:
merging twice fails the exact counts. Bypassing the queue fails the live-peer
check in the merge fake and the observed elapsed wait. Disabling the budget
fails the implementation-timeout control. Freezing, resetting or over-crediting
the budget after waiting fails the post-wait work control. These are behavioral
checks, not a prescribed accounting algorithm or polling interval.

## Failure on main before implementation

HEAD and local main both resolve to
d8c210991281045990bd08373528c41fb4419fc2. No production files were changed.
The targeted `uv run pytest` command above returned exit 1:

```text
E       AssertionError: A completed run must merge after waiting 90s for its turn; got timeout: <!-- foragent:blocked reason=timeout -->
E         **Too big for one session; split it in order**
E       AssertionError: A completed run must merge after waiting 185s for its turn; got timeout: <!-- foragent:blocked reason=timeout -->
E         **Too big for one session; split it in order**
E       AssertionError: The run must reach its turn before the later work consumes the last 20s
E       assert (20 >= 90)
3 failed, 1 passed in 5.32s
```

The passing case is the real implementation-over-budget control. The post-wait
control fails today because the run stops after only 20 seconds of queue waiting,
before it can reach the later active work. The red baseline is intentional;
implementation has not been attempted.

## Order validation in this offline run

`fa order publish --dry-run order-out/plan.json` returned exit 1 because the
isolated GitHub CLI environment requires authentication. No login, credential
change, network lookup or real publication was performed. Dry-run still reads
GitHub issues before returning its preview in the current implementation.

Separately, the same public CLI invocation was run with only `GhTracker`
replaced by the existing `FakeTracker`. It returned exit 0 and previewed one
size-s issue in lane main plus the spec and locked acceptance paths. The fake
tracker had no issues or PRs written. Schema validation, exact equality between
the embedded acceptance source and the standalone file, the issue id/key/size,
the uv Done-when command and the main-lane fallback also passed. This offline
preview is not evidence of successful access to the live GitHub repository.
