# Fix the seams found while dogfooding: CI feedback, conflict budget, collect step

Implements D50, D51 and D52 (docs/decisions.md). All three come from the second order, where issues stopped for reasons a nearer layer could have fixed (D44).

### ci-feedback — D50 CI failures go back to the implementer, who knows the full test command (size m)
Goal: (1) `build_prompt` takes `test_command: str | None = None`. When set, the rules include exactly the line ``- Before you finish, also run the full test suite: `<command>`. CI runs it, so existing tests must keep passing.``; when not set, the line is absent. The runner passes `config.test_command`, falling back to the first command from `order.detect_test_commands(worktree)`, else nothing. (2) The tracker gets `pr_failed_logs(pr) -> str`: the log of the failed checks for the PR head (GhTracker: the failed Actions runs for the head SHA via `gh run view <id> --log-failed`, trimmed to the last 4000 characters per run; FakeTracker: `set_pr_failed_logs(pr, text)`, default ""). (3) When CI fails, in every CI wait loop of the runner, the runner does not block at once. If the attempt budget (`max_retries`) has room, it counts one attempt and sends the implementer back to the same worktree with the failure text `CI failed:\n<log>`, then goes through the same path as a failed Done when check (Done when, review, publish, CI again). Only when the budget is used up does it raise `blocked` with reason `retries-exceeded`, and the block comment includes the log.
Out of scope: Changing how CI status is polled or how long it waits; parsing test names out of the log (the implementer reads the raw log).
Test checks: tests/acceptance/test_ci_feedback.py

### conflict-budget — D51 merge conflicts have their own budget and a rebuild fallback (size m, blocked by ci-feedback)
Goal: (1) New config `conflict_retries: int = 2` (validated like `max_retries`). Conflict resolution attempts in `resolve_conflicts` count against this budget, not `max_retries`, so review rounds can no longer leave a conflict without tries (issue #41). (2) When the conflict budget is used up, the runner rebuilds once: it recreates the issue branch and worktree from the latest target branch, runs the implementer one more time with the original instructions plus a note that the earlier attempt could not be merged onto the moved base, then goes through Done when, review and publish (force-pushing the rebuilt branch with lease). (3) If the rebuild also fails or conflicts again, raise `blocked` with reason `retries-exceeded`. Record the rebuild in the run log.
Out of scope: Changing the merge lock, `base_merges` limit, or batch patch replay.
Test checks: tests/acceptance/test_conflict_budget.py

### ci-collect — D52 CI collects every test, including locked acceptance tests (size s)
Goal: (1) Add a step to `.github/workflows/ci.yml` before the test step: `uv run pytest --collect-only -q` (no `--ignore`), so a test module name clash or import error fails every PR. Keep the existing test step (`--ignore=tests/acceptance`). (2) `adopt.ci(root)` reports a new key `collect_only`: a list of `{"file": <workflow path>, "add": "<pytest prefix> --collect-only -q"}` for each workflow that runs pytest but has no `--collect-only` run; the prefix is the invocation up to and including `pytest` (e.g. `uv run pytest`). Report only; do not edit the workflow file. Show it in the `fa adopt ci` output and in skills/fa-init where CI findings are shown. No name-checking rules in the runner.
Out of scope: Rewriting workflow YAML automatically; other CI providers.
Test checks: tests/acceptance/test_ci_collect.py

## Interface notes fixed by the locked tests

- `build_prompt(..., test_command=...)` keyword; exact sentence above.
- `FakeTracker.set_pr_failed_logs(pr, text)`; `Tracker.pr_failed_logs(pr)`.
- `Config.conflict_retries` default 2.
- `adopt.ci(root)["collect_only"]` shape above.
- In the test harness the first PR is #2 (issue #1 exists).
