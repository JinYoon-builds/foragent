---
name: project-952-silent-push
description: #952 silent-orchestrator push — orch_silent.py, observe_silence shared by gate and fa orch ensure, settings registry pitfalls
metadata:
  type: project
---

- Final design (after #962 seat, 3 failed review rounds): the seat holder `(pid, start_time)` from `orch_seat.read` is the ONLY episode identity; origin = max(heartbeat at, holder taken_at); `orch_seat.alive() is None` counts as live, False/corrupt seat = no episode. Self-built identity tracking (argv scans, heartbeat session, spawn record) was the failing defect class every round — consume the shared owner of the fact instead.
- `orch_spawn.observe_silence(root, store, config, now, reason)` is the one silence step for gate outer/in-lock reasons (`_live_reason`), `ensure_explicit` outer/inner, and the CLI `commands/_orch_box._ensure` (which computes its own skip_reason and never calls ensure on a stop). `orch-silent-alert.json` = `{holder:[pid,start], pushed_at}`; check→send→record and rearm share `orch-silent-alert.lock`; freshness re-read under that lock.
- Mutation runs: back up into an existing in-repo dir and check `cmp` after sed — a missing backup dir made mutations accumulate silently once.
- Adding a Setting to `settings.SETTINGS` also needs `settings_window_label_<key>` (watch settings window KeyErrors otherwise), and updates `tests/test_settings.py` order list and `tests/test_config.py::test_exact_defaults`.
- SKILL.md is at the 150-line cap: reflow the whole paragraph (backtick spans as tokens) to fit a new sentence; compare the 11 skill-reading test files' PASSED set before/after (184 tests). In zsh use an array `F=(...)` for file lists — `$F` does not word-split.

**Why:** these were the non-obvious breakages hit while implementing #952.
**How to apply:** reuse when touching orch silence, settings registry or the orchestrator skill.
