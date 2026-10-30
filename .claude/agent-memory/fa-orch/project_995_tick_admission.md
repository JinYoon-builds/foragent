---
name: project-995-tick-admission
description: #995 resilient tick admission — refresh failure isolation, gh.Budget deadlines, board read only on changed listing, D69 lane conflict
metadata:
  type: project
---

- Refresh failure (non-rate-limit TrackerError) sets `ctx._refresh_failed`, skips `flush_outbox`, and the board loop body is `consider(issue)`; per-issue TrackerError is deferred only when the refresh failed. `_start_checks()` in scheduler_start does one `gh.bounded(5,5)` get_issue.
- `gh.bounded`/`gh.Budget` use a ContextVar and `time.monotonic` through the module, so tests patching `time.monotonic` and `subprocess.run` see them. `GhTracker._call` checks the budget before counting the call; `_run_gh` passes `gh.call_timeout(120)`.
- `refresh_issues()` now returns whether the listing changed; board `refresh_cards` runs only then. #184 had added an unconditional board read after #314, which broke `test_tick_github_budget` since 2026-10-24.
- **Why:** the locked test `test_non_start_failure_keeps_three_cached_admissions...` expects three fileless issues in one lane to start together, which D69 (`overlap_files.admits`: no Files → one per lane) forbids. Left `.foragent/QUESTION.md` recommending a test fix.
- **How to apply:** when a locked test expects several same-lane starts, check Files first; probe by patching `overlap_files.can_start` to isolate the cause.
- Resolved 2026-10-30: the test owner moved #2/#3 to their own lanes; D69 unchanged. The retry also needed `test_tick_rate_limit_persists_wait_but_other_tracker_errors_raise[HTTP 422...]` updated: any unit test expecting a non-rate-limit refresh TrackerError to escape `tick()` belongs to the same superseded class (grep `"list_issues"` patches near `pytest.raises`).
- Review round 2: after a failed refresh the board loop must make no issue GET (new approval / re-approval are deferred; issues repaired by orphan or run recovery wait a tick), every gh subprocess is capped at 5 s via `ctx._fallback` ExitStack, and `_start_ready` starts nothing while `store.github_outbox()` is non-empty (queued READY/RUNNING writes are not delivery).
- CI round: locked `test_log_retention` requires the tick to call `store.prune_logs` exactly once, and `test_tick_admission_resilience` requires one `trim_journals` per tick: housekeeping calls only `prune_logs`, and `trim_journals` leaves `store.trimmed_journals` for the "Trimmed ..." report line. `test_cli.py::test_error_exit_code_and_single_stderr_line[error1]` was another superseded refresh-error test (exit-code form); it now raises from `scheduler._recover_runs`. `test_github_outbox_gate_preserves_and_flushes_rate_limited_close` fails on base too.
