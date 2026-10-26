---
name: project-287-plan
description: #287 lane plan (plan.json) design, CLI error pattern, non-reentrant state lock, and the stall-vs-clock-advance conflict in its locked test
metadata:
  type: project
---

#287: `orch_plan.py` owns `<state>/plan.json` (queues, paused{until,why,by}, width, expires_at, authors, written_by). `orch_queue.get/set_order` delegate to it; lane-queues.json migrates once in the loader. Tick: `orch_plan.refresh()` in `tick()` right after `usage_recheck`, before `github_wait_check` (lapse silently, expire with one `plan-expired`); `_lane_held` also checks plan pauses; `_at_plan_width` in `_start_ready` caps a lane's active runs.

- `store._state_lock()` is a non-reentrant flock. Write a state change and its event under one lock with `orch_events.append_event(..., locked=True)`; journaling after release lost `plan-expired` when a later check refused the edit (review finding).
- Review rule: local upkeep (expiry, lapse) must run before any GitHub-wait early return in `tick()`.
- Acceptance `cli_exit` returns `SystemExit.code` and never prints it: for errors a test reads, print to stderr and `raise SystemExit(1)`.
- In zsh, split a file list with `files=(${(f)"$(grep -rl ...)"})`; `$files` from a string is one argument.

**Why:** `test_lane_width_is_only_a_cap_set_by_a_live_plan` advances the clock 1h with live runs, so the 30-minute stall rule (`stall_minutes`) restarts them. Not fixable in scope; wrote `.foragent/QUESTION.md` (recommend amending the locked test; #435 silence watchdog is the related pending change).
**How to apply:** any locked test that advances a fake clock past 30 min with active runs needs stall handled by the test or by #435 first; check this before blaming new code.

Resolved 2026-10-27: the orchestrator answered the question by adding `stall_minutes=120` to the locked test (option 1). For the SKILL.md 150-line limit, move a whole paragraph verbatim into a file beside it with a one-line pointer (`notify.md` held the push-notification paragraph), then compare the PASSED/FAILED set of every acceptance file that greps `fa-orchestrator` before and after.

Merge with #333 (2026-10-27): `fa orch facts` must never open `plan.json` (locked acceptance guard on `open`), so the facts start-order replay (`orch_facts_resources`) now uses issue-number order instead of lane queues. When merging, keep both `facts` and `pause/resume/plan` in `INBOX_ACTIONS` and parser registration. Check regressions by loading main's `src` via `git archive <sha> src` into `.foragent/base` and `PYTHONPATH=...`, then diff the FAILED sets.

Line limits (2026-10-27): `scheduler.py` ≤ 250 (`test_scheduler_split`, in the runner's baseline), every `src/foragent/*.py` and `commands/*.py` ≤ 300 (`test_cli_split`). Plan and queue CLI code lives in `orch_plan_cli.py` (lane and global queue, pause/resume/plan); `commands/orch.py` imports it inside functions. Before finishing, run every `tests/acceptance/test_*_split.py`.
