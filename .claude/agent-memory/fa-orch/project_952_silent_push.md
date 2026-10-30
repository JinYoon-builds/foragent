---
name: project-952-silent-push
description: #952 silent-orchestrator push — orch_silent.py, observe_silence shared by gate and fa orch ensure, settings registry pitfalls
metadata:
  type: project
---

- `orch_spawn.observe_silence(root, store, config, now, reason)` is the one silence step; the gate path and `orch_ensure.ensure_explicit` both call it with the skip reason ("alive" rearms, "session" notes hourly + `orch_silent.check`). FORAGENT_ORCH_SPAWN=off returns before it.
- `orch-silent-alert.json` = open episode `{seen: [identities], pushed_at}`. Identity = `spawn:<spawned_at>` or `process:<pid>:<start>` (scan + alive recorded session). New episode only when an identity not in `seen` appears (even if it began before the push); missing reads change nothing; `seen` accumulates from the first silent check. Review rounds rejected both "compare whole identity" (loss re-pushes) and "start > pushed_at" (misses changed sessions).
- Freshness must be re-read where the decision is made: `check` re-reads the heartbeat under `orch-silent-alert.lock` and rearms when fresh; `observe_silence` re-checks `_alive` before the "session" note. Check→send→record and rearm share that lock; never hold the state lock across send.
- Adding a Setting to `settings.SETTINGS` also needs `settings_window_label_<key>` (watch settings window KeyErrors otherwise), and updates `tests/test_settings.py` order list and `tests/test_config.py::test_exact_defaults`.
- SKILL.md is at the 150-line cap: reflow the whole paragraph (backtick spans as tokens) to fit a new sentence; compare the 11 skill-reading test files' PASSED set before/after (184 tests). In zsh use an array `F=(...)` for file lists — `$F` does not word-split.

**Why:** these were the non-obvious breakages hit while implementing #952.
**How to apply:** reuse when touching orch silence, settings registry or the orchestrator skill.
