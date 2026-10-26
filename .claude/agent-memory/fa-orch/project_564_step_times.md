---
name: project-564-step-times
description: #564 tick step timings — orch_tick_outcome.step/timed contextvar, scheduler.py 250-line cap workaround
metadata:
  type: project
---
- `orch_tick_outcome.step(name)`/`timed(name, fn, *args)` read a ContextVar set by `run_recorded`; outside it they only run the step. Steps land in `tick-outcome.json` `steps`; ≥60 s prints `tick_step_slow` (messages/tick_steps.py) to stderr.
- `tests/acceptance/test_scheduler_split.py` caps scheduler.py at 250 lines. Re-export new names (`step`, `timed`, `_reopen_view`) via the existing `scheduler_start` import block instead of new import lines; `_reopen_view` moved to scheduler_start.py.
- Acceptance tests patch `time.monotonic` globally, so duration and steps share the fake clock.
- Review lesson: "every top-level step" means every I/O call in `tick`/`_tick_locked`, including early-return paths (GitHub-wait budgets, rate-limit pause, idle `_machine_budget`, tidy, process snapshot/prune in `scheduler_stop.tick_processes`). GitHub-wait checks go through `scheduler_start._github_wait(ctx, ready, name)` / `_github_pause`, which call `scheduler._apply_budgets` so facade patches still apply.
