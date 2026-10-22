# Stall test findings

Implements D54.

### stall-recovery (size s)
Goal: Implements D54 (1) and (2) (docs/decisions.md D54).
(1) When the scheduler finds an active run whose runner pid no longer exists (crash), it first calls `ctx.kill_runner(run.pid)` (the default `kill_process_group` ends the whole group; orphaned executors share the runner's process group), then removes the worktree and restarts as today.
(2) Every automatic recovery the gate performs is appended to the orchestrator event log (`orch-events.jsonl` in the state dir, same record shape the orchestrator verbs use: `at`, `issue`, `kind`, `summary`) so `fa gate watch` shows it under "handled": kinds `stall-restart`, `crash-restart`, `stalled` (blocked after the second stall) and `orchestrator-timeout` (escalated to needs-human). Summaries are short and human-readable (what happened, what the gate did).
Test checks: tests/acceptance/test_stall_recovery.py

### stall-visibility (size s)
Goal: Implements D54 (3) and (4) (docs/decisions.md D54).
(3) In `fa gate watch` (live and `--once`), a working run shows the stalled mark (`○ ...`) when its last activity is older than min(10 minutes, `config.stall_minutes`), and immediately when the run's runner pid no longer exists (show it as stalled/runner gone, in both languages).
(4) The notification line sent by `inspect notify` names what needs the person: after the existing counts, add the needing-human items as `#<n> <title>` (at most three, then `+N`), with the reason when known. Keep it one line.
Test checks: tests/acceptance/test_stall_visibility.py
