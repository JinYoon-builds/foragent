---
name: project-413-refresh
description: #413 orchestrator refresh — heartbeat session fields, spawn split, locked --bg argv conflict, zsh/skill-limit pitfalls
metadata:
  type: project
---

- `orch-heartbeat.json` carries the active session (`session_id`, `transcript_path`, `started_at`, `status`, `previous`). `orch_sessions.activate()` is the only writer of these; `StateStore.write_heartbeat` keeps them, ignores writes whose `session_id` differs, and takes the state lock on the orchestrator side.
- `orch_spawn.py` hit the 300-line cap (test_cli_split), so launch steps live in `orch_spawn_launch.py`, re-exported at the bottom of `orch_spawn` (circular import, call-time lookup). `orch_refresh` calls `orch_spawn.spawn_orchestrator` through the module so the locked fixture's patch works.
- Every launch (gate/ensure/refresh; --bg/Orca/tmux) names a new uuid via `--session-id`, sets `FORAGENT_SESSION_ID` itself (never the inherited one), and `orch_spawn.adopt` writes the descriptor into the heartbeat via `orch_sessions.activate` inside the opener lock. The test owner changed `test_orch_spawn.py` (2026-10-30) and `test_orch_spawn_liveness.py` (2026-10-30) to require this after two SPEC-CONFLICT stops; the interim workarounds (self-claim via `CLAUDE_CODE_SESSION_ID`, deriving the active session from `orch-spawn.json`) were deleted. Lesson: when a locked test blocks the Goal, raise SPEC-CONFLICT/QUESTION early instead of building a workaround.
- Every orchestrator launch and activation runs inside `watch_open._opening` (flock, not reentrant across fds in one process, so `spawn_orchestrator` takes no lock and `refresh` holds it). Review rejected check-then-act across lock release.
- New CLI help must follow repo language: reuse `commands/report._help_language` + `_repo_root_in` and relocalize in a wrapped `parse_known_args`.
- CLI refusals must raise an exception that `cli.main` catches (subclass `orch_inbox.InboxError`); a `SystemExit(msg)` prints nothing when tests call `main()` directly.
- SKILL.md is already at exactly 150 `splitlines()` (wc shows 149), so a new pointer needs a reflowed paragraph, not an extra line.
- zsh does not word-split `$T`; use `${=T}` for a list of test files, or the run silently says "no tests ran".

**Why:** these cost retries in #413. **How to apply:** for later orch-session or spawn work, start from these seams.
