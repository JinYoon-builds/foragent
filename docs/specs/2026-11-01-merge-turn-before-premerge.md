# Take the human merge turn before premerge

## Request

On 2026-11-01 the PM reported that `fa orch merge 1298 --approved-by ...`
ran protected premerge tests for 28 minutes before reporting that #1264 held
main's merge turn. A human should learn about that holder immediately, before
spending time or machine capacity on premerge work.

## Root cause observed on main

Baseline: `1ff7aab3294bd1edfbe023e9281072993af91aa9` (`HEAD` equals local `main`).
In `src/foragent/orch.py`, `merge()` calls `_premerge()` before attempting the
shared merge turn. `_premerge()` prepares the merge result and launches the
protected test runner, so all of that expense happens before contention is known.
`src/foragent/run_merge_turn.py:claim()` already checks live entries atomically.
Its documented human reservation has no live run record; its protection relies
on the caller retaining the shared merge reservation through the operation.
The gap is when the human merge obtains that reservation and how long it keeps
it. This is coordination of existing state, with no new judgment policy.

## Required behaviour

- A human `fa orch merge` first establishes whether it can take the PR target's
  merge turn. If another live run holds that target, return a nonzero result
  promptly, identify the holder issue and target, and invoke no premerge test
  runner. This also applies while the holder is already merging.
- Once the target's turn is held, perform the existing premerge checks. A ready
  PR proceeds to merge its checked head while retaining that same turn through
  the merge. The command releases its reservation when it finishes.
- A live holder for a different target does not make this target busy.

Close when the locked public-CLI acceptance file passes using the Done when
command. Internal structure, helper names and the implementation approach belong
to the lane.

## Ownership and scope

One issue, size `s`, lane `orch`, which owns `src/foragent/orch*.py` in
`.foragent.yml`. Expected implementation file: `src/foragent/orch.py`.
The human merge command continues to own its reservation; other runs continue
to own their existing queue entries. No new shared state or writer is requested.

Out of scope: automated-run queue policy and priority, the standalone
`fa orch premerge` command, protected-suite selection or baseline semantics,
CI/readiness policy, new retry or scheduling rules, and changes to agent judgment.

## Locked acceptance

The file is supplied in `acceptance_files` for publication under
`tests/acceptance/test_merge_turn_before_premerge.py`. Its review copy is
`order-out/tests/acceptance/test_merge_turn_before_premerge.py`.
All four cases call `foragent.cli.main` with `orch merge` arguments, the public
entry point registered for `fa`. Real premerge orchestration, live run records,
queue claims and shared merge locking execute. Git/GitHub transport, the suite
process and machine test capacity are offline fakes. The test observes the shared
reservation at the suite-process and merge transport boundaries; it does not
replace premerge or turn-claim logic.

Cases:
1. A ready live holder on main is named; zero suites and zero merges occur.
2. A live holder on release already owns the shared merge lock; the command
   reports that holder while its lock remains occupied and launches zero suites.
3. With an empty turn, one configured protected suite runs and the checked head
   merges exactly once under the same reservation.
4. A live holder on release leaves main available for that checked merge.

The two-second refusal bound is an offline test watchdog, not a production
latency setting. No real protected suite or network request is made.

Done when, from the repository root after publication:

```sh
uv run pytest tests/acceptance/test_merge_turn_before_premerge.py -q
```

## Break-it review

This is an oracle review, not a claim that mutation testing was run.
- Wrong result or holder: the busy cases require nonzero status plus the holder
  issue and target; the available cases require success and a merged PR.
- Skip an effect: the available cases require exactly one suite invocation and
  exactly one merge of the expected checked head, after the suite.
- Do an effect twice: exact suite and merge counts reject duplicate execution.
- Run premerge before refusal: both busy cases require zero suite invocations.
- Release the turn early or reacquire it later: the suite and merge boundaries
  must observe one uninterrupted reservation.
- Merely put expensive work behind an occupied merge lock: the merging-holder
  case must return before that lock is released.
- Treat every branch as busy: the other-target case must still merge main.

## Red evidence on unchanged main

Network was disabled. The existing local dependency environment was reused with
`UV_OFFLINE=1`, `UV_NO_SYNC=1` and `PYTHONPATH` pointing to this checkout's `src`.
Caches, temporary HOME directories and test output were confined to the working
directory. Plugin autoload was disabled; no pytest-xdist was used.

Actual targeted command:

```sh
uv run pytest order-out/tests/acceptance/test_merge_turn_before_premerge.py -q -p no:cacheprovider --basetemp="$PWD/.run-tmp/pytest-final"
```

Exit status: 1. Failure lines copied from that run:

```text
E       AssertionError: Busy merge turn must be refused before invoking any premerge test runner
E       assert [None] == []
E       AssertionError: Report the busy turn while its holder still owns the merge lock
E       assert not True
E       AssertionError: Premerge test runner started before the merge turn was held
E       assert None is not None
E       AssertionError: Premerge test runner started before the merge turn was held
E       assert None is not None
FAILED order-out/tests/acceptance/test_merge_turn_before_premerge.py::test_live_holder_is_named_without_running_premerge[ready-holder]
FAILED order-out/tests/acceptance/test_merge_turn_before_premerge.py::test_live_holder_is_named_without_running_premerge[holder-merging]
FAILED order-out/tests/acceptance/test_merge_turn_before_premerge.py::test_available_target_runs_premerge_and_merges_under_one_turn[empty-turn]
FAILED order-out/tests/acceptance/test_merge_turn_before_premerge.py::test_available_target_runs_premerge_and_merges_under_one_turn[other-target-busy]
4 failed in 2.59s
```

The ready-holder case reached the expected holder diagnostic but had already
invoked the suite. The merging-holder case waited for the occupied lock. Both
available cases ran their suite and merged, but the suite ran before obtaining
the reservation. These are requirement failures, not fixture or import failures.
