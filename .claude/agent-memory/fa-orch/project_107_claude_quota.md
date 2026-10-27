---
name: project-107-claude-quota
description: #107 Claude keep/burn quota — where the switch lives, line-limit traps, test clock for expired windows
metadata:
  type: project
---

- `scheduler_quota.py` holds `claude_allowed`/`claude_percent`/`switch_reason`/`switched_config`/`lane_start`/`record_switch`. `scheduler.py` re-exports `claude_allowed` in one line (facade limit 250; it was 249).
- Window expiry uses `store._now()` (the injected clock), not real time: the acceptance fixture's five_hour reset (2026-10-22) is already past in real time.
- `_lane_held` checks the switched executor pair, so "Codex paused too → lane waits" and `orch facts` follow it.
- Tick writes `Run.implementer` once via `start_run(implementer=...)`; runner applies `switched_config(lane_config(...), run.implementer, claude paused)`. Restarts decide afresh.
- `state_runs.py` is at 298/300 lines: add params on existing lines.
- `fa init` passes `claude_quota` only when the flag is given (fake `init_repo`s in other tests may not accept it).
- `tests/acceptance/test_runner_split.py` fails on base (capacity.py, run_publish.py over 600) — not ours.

**Why:** keep these traps findable when the quota feature grows (#186 headroom choice is out of scope here).
**How to apply:** extend `scheduler_quota.py` rather than scheduler.py/scheduler_start.py.
