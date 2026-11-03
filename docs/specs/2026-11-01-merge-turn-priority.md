# Merge turns follow cross-lane priority (2026-11-01)

## Reported problem and root cause

The PM's 2026-10-31 incident report says #1295, the global-priority-1 fix for a main-wide failing test, waited more than 65 minutes in merge-lock-wait behind runs whose merges failed for that same test. #1168 also retained its turn through about 17 minutes of CI waiting. These incident timings come from the supplied request, not a live lookup.

At the supplied main revision 10aad1a8f3fa76ffc21d83b283e21b9c899709c3, src/foragent/scheduler_start.py reads orch_queue.get_global to rank start candidates. src/foragent/run_merge_turn.py independently scans active join records in insertion order in holds_turn, without consulting that existing cross-lane list. The two decision points therefore apply different ordering to the same issues. The agent already supplied the priority; the merge-turn reader lacks that context. This is a missing input at the ordering boundary, not a need for a new semantic judgment rule.

There is a second gap within the requested withdrawal contract: holds_turn returns True after exhausting the active queue even if the queried seat has withdrawn. With all entries withdrawn it grants the turn to both live runs. The locked test exercises this empty-queue edge as well as a withdrawn entry beside an active contender.

## Scope

One unattended medium issue covers merge-turn ordering. The issue Goal states the required behavior; implementation choices remain with the lane. The existing cross-lane priority list and join records remain authoritative, with no new persisted state. Merge failures retain the existing withdrawal/rejoin lifecycle. CI wait duration, turn release during CI, actual in-flight merge locking, merge groups and start admission are outside this order.

## Ownership and assumptions

- No path pattern in the supplied .foragent.yml matches src/foragent/run_merge_turn.py. Use the implicit main lane rather than inventing an orch ownership entry or changing configuration.
- 'On main' means the supplied worktree's main revision: HEAD and refs/heads/main both named 10aad1a8f3fa76ffc21d83b283e21b9c899709c3 before testing. No branch movement or implementation is part of this order.
- The WHAT requests priority among ready runs. The reported CI wait is context, not an additional request to release or preempt an in-flight merge.
- An absent or cleared global list is the empty-list case. Unlisted ready runs share the fallback priority and retain join order. A failed run's later ready entry has a fresh join position within its priority.

## Locked checks and break-it review

The standalone draft is order-out/tests/acceptance/test_merge_turn_priority.py. Its exact contents are embedded under tests/acceptance/test_merge_turn_priority.py in acceptance_files, so publication puts the locked file under the configured acceptance directory. Done when runs the published path using uv run pytest.

The fake store reuses real StateStore queue persistence and locking but supplies live run records in memory. Tests invoke the public seat, join, holds_turn and withdraw entry points and set the real global list with orch_queue.set_global. They do not patch turn-selection internals or require a new helper, record format or algorithm.

Four test functions produce nine concrete cases. They check [B] after A then B join, the empty-list FIFO control, priority-list order when both issues are listed, a later listed join ahead of two unlisted runs, current-list changes without rejoining, and withdrawals including an empty active queue. Issue numbers oppose join order, and every observation checks all contenders for exactly one holder (or none after all withdraw).

Break-it review: FIFO-only selection fails the listed-later cases; numeric sorting fails the empty-list control; treating listed entries as one bucket fails the [B, A] case; ignoring later joins fails the three-run case; retaining a stale priority fails the current-list case; constant True/False and retaining withdrawn seats fail the holder-set assertions. The read-only check rejects additional persisted state written while deriving a turn. These are reviewed breaks, not a claim that a mutation suite was run.

## Main failure evidence

The exact targeted invocation was:

```sh
HOME="$PWD/order-out/.runtime/home" XDG_CONFIG_HOME="$PWD/order-out/.runtime/home/.config" XDG_DATA_HOME="$PWD/order-out/.runtime/home/.local/share" XDG_CACHE_HOME="$PWD/order-out/.runtime/home/.cache" TMPDIR="$PWD/order-out/.runtime/tmp" UV_CACHE_DIR="$PWD/order-out/.runtime/uv-cache" UV_PROJECT_ENVIRONMENT=/Users/apple/Dev/foragent/.venv UV_NO_SYNC=1 UV_OFFLINE=1 UV_PYTHON_DOWNLOADS=never PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH="$PWD/src" uv run pytest -q -p no:cacheprovider --basetemp="$PWD/order-out/.runtime/pytest-priority" order-out/tests/acceptance/test_merge_turn_priority.py
```

It ran against the unchanged main source and exited 1 on behavioral assertions, with no collection or setup errors. The installed virtual environment was reused without syncing; HOME, caches and temporary writes were confined to a disposable directory inside this worktree. No network or pytest-xdist was used. Pasted failure lines:

```text
E       AssertionError: Expected merge-turn holders [4101], got [4102]
E       AssertionError: Expected merge-turn holders [4101], got [4102]
E       AssertionError: Expected merge-turn holders [4103], got [4102]
E       AssertionError: Expected merge-turn holders [4101], got [4102]
E       AssertionError: Expected merge-turn holders [], got [4102, 4101]
FAILED order-out/tests/acceptance/test_merge_turn_priority.py::test_ready_runs_follow_global_list_then_join_order[listed-later]
FAILED order-out/tests/acceptance/test_merge_turn_priority.py::test_ready_runs_follow_global_list_then_join_order[list-order-beats-fifo]
FAILED order-out/tests/acceptance/test_merge_turn_priority.py::test_listed_run_joining_later_precedes_earlier_unlisted_runs
FAILED order-out/tests/acceptance/test_merge_turn_priority.py::test_existing_ready_runs_follow_the_current_priority_without_rejoining
FAILED order-out/tests/acceptance/test_merge_turn_priority.py::test_withdrawn_runs_never_hold_the_turn[all-withdrawn]
5 failed, 4 passed in 0.14s
```

## Regression and dry-run evidence

The existing merge-turn regression suite was run separately against unchanged main:

```sh
HOME="$PWD/order-out/.runtime/home" XDG_CONFIG_HOME="$PWD/order-out/.runtime/home/.config" XDG_DATA_HOME="$PWD/order-out/.runtime/home/.local/share" XDG_CACHE_HOME="$PWD/order-out/.runtime/home/.cache" TMPDIR="$PWD/order-out/.runtime/tmp" UV_CACHE_DIR="$PWD/order-out/.runtime/uv-cache" UV_PROJECT_ENVIRONMENT=/Users/apple/Dev/foragent/.venv UV_NO_SYNC=1 UV_OFFLINE=1 UV_PYTHON_DOWNLOADS=never PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH="$PWD/src:$PWD/tests" FORAGENT_UPDATE_CHECK=off uv run pytest -q -p no:cacheprovider --basetemp="$PWD/order-out/.runtime/pytest-regression" tests/test_run_merge_turn.py
```

Actual result (exit 0):

```text
10 passed in 9.72s
```

This includes the existing failed-merge withdrawal/rejoin coverage. The new acceptance tests remain failing until implementation. The published-path Done when command is for the future implementation; the draft-path acceptance invocation and regression invocation above are what actually ran here.

The required native command ran with local src on PYTHONPATH, HOME/XDG/GH_CONFIG_DIR/TMPDIR isolated inside this worktree, tokens unset and update checks disabled:

```sh
fa order publish --dry-run order-out/plan.json
```

It exited 1 before obtaining the GitHub-backed preview:

```text
foragent: To get started with GitHub CLI, please run: gh auth login Alternatively, populate the GH_TOKEN environment variable with a GitHub API authentication token.
```

An additional offline preview uses the same local CLI entry point with only its GhTracker constructor replaced by the repository's existing empty FakeTracker. The plan parser, publisher, schema checks and repository configuration are real; no production files or publishing logic are replaced. This preview cannot verify existing GitHub orders or duplicates. It performs no actual publication or approval. Runtime files stay inside order-out/.runtime and are removed before delivery.


Offline preview invocation (with the same confined environment and offline uv settings as above):

```sh
uv run python - <<'PYTHON'
from foragent import cli
from foragent.tracker import FakeTracker
cli.GhTracker = lambda repo: FakeTracker()
raise SystemExit(cli.main(['order', 'publish', '--dry-run', 'order-out/plan.json']))
PYTHON
```

The offline CLI preview exited 0. Its actual output was:

```text
Plan (dry run): Give ready merge turns the cross-lane priority
Files: docs/specs/2026-11-01-merge-turn-priority.md, tests/acceptance/test_merge_turn_priority.py

lane:main
| Number | Title | Size | Blocked by | Approved? | Interactive? |
| --- | --- | --- | --- | --- | --- |
| merge-turn-priority | Honor cross-lane priority when ready runs take a merge turn | m | — | no | no |
```

Local schema validation, Python AST parsing, checking the configured lane path patterns and exact equality of the embedded and standalone acceptance contents also passed. No implementation, existing-issue edits, new lanes, commits, pushes, resets, checkouts or stashes were performed. Only the plan and its standalone locked acceptance draft are delivered.
