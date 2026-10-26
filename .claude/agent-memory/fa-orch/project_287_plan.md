---
name: project-287-plan
description: #287 lane plan (plan.json) design, CLI error pattern, non-reentrant state lock, and the stall-vs-clock-advance conflict in its locked test
metadata:
  type: project
---

#287: `orch_plan.py` owns `<state>/plan.json` (queues, paused{until,why,by}, width, expires_at, authors, written_by). `orch_queue.get/set_order` delegate to it; lane-queues.json migrates once in the loader. Tick: `orch_plan.refresh()` after `github_wait_before_start` (lapse silently, expire with one `plan-expired`); `_lane_held` also checks plan pauses; `_at_plan_width` in `_start_ready` caps a lane's active runs.

- `store._state_lock()` is a non-reentrant flock; `orch_events.append_event` takes it too, so journal events only after releasing the lock.
- Acceptance `cli_exit` returns `SystemExit.code` and never prints it: for errors a test reads, print to stderr and `raise SystemExit(1)`.
- In zsh, split a file list with `files=(${(f)"$(grep -rl ...)"})`; `$files` from a string is one argument.

**Why:** `test_lane_width_is_only_a_cap_set_by_a_live_plan` advances the clock 1h with live runs, so the 30-minute stall rule (`stall_minutes`) restarts them. Not fixable in scope; wrote `.foragent/QUESTION.md` (recommend amending the locked test; #435 silence watchdog is the related pending change).
**How to apply:** any locked test that advances a fake clock past 30 min with active runs needs stall handled by the test or by #435 first; check this before blaming new code.
