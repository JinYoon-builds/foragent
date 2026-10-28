---
name: project-129-blocked-push
description: #129 gate push for blocked/needs-human issues and orchestrator outage — where it hooks into the tick and how dedupe/re-arm works
metadata:
  type: project
---

- `orch_push.after_tick(ctx, spawned)` runs in `orch_spawn.deciding()` after the spawn decision (`_spawn()` returns the screen; `on_tick` wraps it). Only when `ctx._push_ready` is true: `scheduler.tick()` resets it, `_tick_locked()` sets it after `github_wait_after_refresh`, the rate-limit except path clears it; the exception path of `deciding` never pushes.
- Blocked set = open cached issues (`local_issue_models`) with blocked/needs-human labels. Reason: flag reason, or for needs-human-only the newest `escalate` event summary, else "".
- `<state>/notify-blocked.json` (only writer: orch_push) saves the set seen last; due = no `push-blocked` record, reason differs from the record's `reason` field, or absent from the saved set. Due-but-unsent issues are left out of the saved set so they stay due.
- Outage: active runs + stale/missing heartbeat (ALIVE_SECONDS) + not `orch_spawn._session_alive`. `orch_off_pushed` resets only on a fresh heartbeat.
- Tests: `tests/acceptance/test_notify_blocked.py` (3), `tests/test_orch_push.py` (12). Unit tests patch `foragent.orch_spawn._session_alive` and create runs with `start_run(n, branch=, worktree=)` (update_run needs an existing run).
- The /goal says no files outside the repo may change: keep test logs inside the worktree or delete /tmp files afterwards.
- Review fix (defect class: a later failure discards an earlier successful effect): `ensure_orchestrator` now swallows `_record_session` and spawn `notify.send` errors after a successful launch so the screen still reaches `deciding` (pushes suppressed that tick). `after_tick` writes each journal record and the state file independently after a sent push, then raises the first error. Tests patch `orch_binding.record_tick`/`orch_stale_files.report` to drive `deciding` directly.
- Review fix (defect class: user-visible text outside the messages registry): tick report lines now come from `messages/push_blocked.py` (`push_blocked_tick`, `push_blocked_tick_orch_off`, `push_blocked_failed`) via `orch_push.tick_line()`; `after_tick` returns `(issues, outage)` facts, not text. Journal summaries and label/reason identifiers stay English by design (REBUTTAL).
- Retry 4: scheduler.py has a 250-line cap (test_scheduler_split). Push readiness now lives outside it: `orch_spawn.deciding` resets `ctx._push_ready`, `orch_push.refresh_waited(ctx, _github_wait(...))` wraps the post-refresh wait line, `scheduler_start._github_pause` clears it. Full acceptance compare vs merge-base b5f22cd found 7 tests only failing with the feature (test_parent_follows_children forbids notify.send with blocked children; test_reconcile counts every orch-events line with a heartbeat-less run) → QUESTION.md. Compare tip: run failing *files* on the base (`PYTHONPATH=<extracted>/src`), since -rf IDs escape non-ASCII params and can't be reselected.
