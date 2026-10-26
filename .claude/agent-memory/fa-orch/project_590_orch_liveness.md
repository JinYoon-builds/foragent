---
name: project-590-orch-liveness
description: #590 orchestrator liveness past a stale heartbeat — session record, /fa-orchestrator scan, detached Done when check under Claude
metadata:
  type: project
---
- `processes.claude_session()` walks `psutil.Process(os.getpid()).parent()` to the nearest argv[0]=claude / name claude(.exe), with NO cwd filter. A first attempt filtered by repo-root cwd so test_heartbeats' exact `{"at"}` held under a real Claude; review rejected it as narrowing production to fit fixtures. The final design stops the walk at a Foragent gate process (`fa gate`/`-m foragent.cli gate`) and records no session. **Why:** the stop hook's `gate check-done` runs Done when under the lane executor `claude -p --agent fa-orch`, and that Claude is not an orchestrator. **How to apply:** verify with the real `python -m foragent.cli gate check-done --snapshot <approvals/N.json> --worktree . --acceptance-dir tests/acceptance/`, not bare pytest from the Bash tool.
- `orch_spawn._session_alive()` = heartbeat `session` matched by `recorded_identity_evidence(fresh=True)` OR `processes.orchestrator_process_alive()` (argv has /fa-orchestrator, cwd read only for candidates). Note throttle file `orch-silent-note.json`, event kind `orch-silent`.
- Spawn record gets `pid`/`start_time` only if `subprocess.run` result has an int `pid` (the acceptance fake sets it; real CompletedProcess has none).
- Skill-reading acceptance files (10) compared before/after the SKILL.md edit: identical PASSED/FAILED sets (27 passed).
- Editing processes.py makes the runner run tests/test_processes.py. Its `test_large_prompt_reaches_slow_reader_without_blocking_identity_recording` failed whenever run_logged took >1s: the observer thread calls `observe_tree(..., shared=, not_before=)` but the fake took only `(identities, roots)`. It also failed 2 of 3 runs on the base source. Fixed by matching the fake's signature; it then passed 3 of 3 runs.
