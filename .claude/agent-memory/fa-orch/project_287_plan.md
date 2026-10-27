---
name: project-287-plan
description: #287 lane plan (plan.json) design, CLI error pattern, non-reentrant state lock, and the stall-vs-clock-advance conflict in its locked test
metadata:
  type: project
---

#287: `orch_plan.py` owns `<state>/plan.json` (queues, paused{until,why,by}, width, expires_at, authors, written_by). `orch_queue.get/set_order` delegate to it. D95 single writer: only commands write plan.json (first write moves lane-queues.json in and deletes it); readers fall back to lane-queues.json and derive the plan in force (expired → empty, past pauses ignored). Tick: `orch_plan.record_expiry()` in `tick()` before `github_wait_check` appends one `plan-expired` per `expires_at`, deduped from the journal; `_lane_held` also checks plan pauses; `_at_plan_width` in `_start_ready` caps a lane's active runs.

- `store._state_lock()` is a non-reentrant flock. Write a state change and its event under one lock with `orch_events.append_event(..., locked=True)`; journaling after release lost `plan-expired` when a later check refused the edit (review finding).
- Review rule: local upkeep (expiry, lapse) must run before any GitHub-wait early return in `tick()`.
- Acceptance `cli_exit` returns `SystemExit.code` and never prints it: for errors a test reads, print to stderr and `raise SystemExit(1)`.
- In zsh, split a file list with `files=(${(f)"$(grep -rl ...)"})`; `$files` from a string is one argument.

**Why:** `test_lane_width_is_only_a_cap_set_by_a_live_plan` advances the clock 1h with live runs, so the 30-minute stall rule (`stall_minutes`) restarts them. Not fixable in scope; wrote `.foragent/QUESTION.md` (recommend amending the locked test; #435 silence watchdog is the related pending change).
**How to apply:** any locked test that advances a fake clock past 30 min with active runs needs stall handled by the test or by #435 first; check this before blaming new code.

Resolved 2026-10-27: the orchestrator answered the question by adding `stall_minutes=120` to the locked test (option 1). For the SKILL.md 150-line limit, move a whole paragraph verbatim into a file beside it with a one-line pointer (`notify.md` held the push-notification paragraph), then compare the PASSED/FAILED set of every acceptance file that greps `fa-orchestrator` before and after.

Merge with #333 (2026-10-27): review rejected dropping lane queues from the facts replay (weakened `test_replay_follows_the_lane_queue_order`); never change a main test's assertions to fit. Facts read through `orch_queue.ordered` → `orch_plan.load`. Resolved (option 1): the locked guard now allows opening plan.json; `orch_facts._copy_plan` copies only `orch_plan.plan_fields()` into the snapshot, never a goal field. When merging, keep both `facts` and `pause/resume/plan` in `INBOX_ACTIONS` and parser registration. Check regressions by loading main's `src` via `git archive <sha> src` into `.foragent/base` and `PYTHONPATH=...`, then diff the FAILED sets.

Line limits (2026-10-27): `scheduler.py` ≤ 250 (`test_scheduler_split`, in the runner's baseline), every `src/foragent/*.py` and `commands/*.py` ≤ 300 (`test_cli_split`). Plan CLI code lives in `orch_plan_cli.py` (lane queue, pause/resume/plan); main split the box handlers into `commands/_orch_box.py`, which calls it (global queue stays there). main's SKILL.md is exactly 150 lines: the plan.md pointer needs one paragraph rewrapped (width 105, the watch paragraph). Never chain `git show > file` after a step that can fail; it overwrote SKILL.md once. Before finishing, run every `tests/acceptance/test_*_split.py`.
