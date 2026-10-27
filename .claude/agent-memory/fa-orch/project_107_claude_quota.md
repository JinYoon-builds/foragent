---
name: project-107-claude-quota
description: #107 Claude keep/burn quota — where the switch lives, line-limit traps, test clock for expired windows
metadata:
  type: project
---

- `scheduler_quota.py` holds `claude_allowed`/`claude_percent`, a one-read `Snapshot`, `switch_for`→`Switch(reason, key, values)`, `decide()`→`Start(config, implementer, switch, held)` (used by `_lane_held` and `_start_ready`), and `switch_journal` passed as `start_run(journal=...)` so the run record and its executor-switch event share one state lock. The GitHub-limit branch stores only a kept implementer, never a fresh switch. Report line goes through `switch_reason_keep/limit` messages; the event reason stays English. `scheduler.py` re-exports `claude_allowed` through its `scheduler_start` import list (facade is 250/250 after main's plan merge).
- Window expiry uses `store._now()` (the injected clock), not real time: the acceptance fixture's five_hour reset (2026-10-22) is already past in real time.
- Orchestrator answer (2026-10-27): Claude hard-limit pause switches in keep AND burn; reason `Claude paused at its limit until <t>`. `_lane_held` skips a lane pause only when switched and `claude_caused` (Claude paused + lane pair has Claude), leaving the pause file unchanged. Switched runs never write lane pauses (`note_limit`/`hold_lane` get lane=None). Lane `reviewer` key is supported in validation, `lane_config`, `lane_executors`.
- Retry 5 (orchestrator hint): Claude's hard-limit pause moves EVERY Claude role (implementer and/or reviewer) to free Codex in keep and burn. `switched_config` applies the reviewer move even with implementer None (runner re-applies it at run time); only implementer switches are stored and journaled. `decide()` skips a Claude-caused lane pause whenever the run config has no Claude role. Tests cover default pair, Claude-implements and Claude-only lanes.
- `_lane_held` checks the switched executor pair, so "Codex paused too → lane waits" and `orch facts` follow it.
- Tick writes `Run.implementer` once via `start_run(implementer=...)`; runner applies `switched_config(lane_config(...), run.implementer, claude paused)`. A continued run (previous outcome limit-wait/crashed/stalled-restart) keeps its stored implementer via `scheduler_quota.run_start`/`kept_implementer` — the tick's GitHub-limit branch and the orphan-crash record in scheduler_recovery pass it too; only implementer None chooses afresh. `_start_ready` also skips a start whose own pair is paused (`pair_paused`).
- `state_runs.py` is at 298/300 lines: add params on existing lines.
- `fa init` passes `claude_quota` only when the flag is given (fake `init_repo`s in other tests may not accept it).
- `tests/acceptance/test_runner_split.py` fails on base (capacity.py, run_publish.py over 600) — not ours.

**Why:** keep these traps findable when the quota feature grows (#186 headroom choice is out of scope here).
**How to apply:** extend `scheduler_quota.py` rather than scheduler.py/scheduler_start.py.
- The runner recreates the worktree between attempts; `.foragent/TRY.md` must be rewritten after a retry.
- CI 2026-10-27 09:15Z: `tests/test_cli.py` app fixture pinned tracker/store to NOW (10-20) but not `cli.datetime`; inspect's 7-day window expired → 2 failures on main too. Fixed by pinning `cli.datetime` in the fixture. Read CI logs with `gh run view <id> --log-failed` after the whole run completes.
