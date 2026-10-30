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
