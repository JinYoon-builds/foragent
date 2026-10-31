---
name: project-590-orch-liveness
description: #590 orchestrator liveness past a stale heartbeat — session record, /fa-orchestrator scan, detached Done when check under Claude
metadata:
  type: project
---
- `processes.claude_session()` records the nearest ancestor with argv[0] basename claude (or name claude/claude.exe) literally, whatever lies between (cwd, gate commands). Two rejected attempts narrowed it (repo-root cwd filter, then stop at `fa gate`) so locked test_heartbeats' exact `{"at"}` would hold when pytest runs under a real Claude (gate check-done runs under the lane executor). **Why:** review called both production special-casing of the verification context. **How to apply:** isolate in tests instead — `tests/conftest.py::no_real_ancestors` makes `psutil.Process(<pytest pid>).parent()` return None; fake-table tests override psutil.Process after it.
- `orch_spawn._session_alive()` = heartbeat `session` matched by `recorded_identity_evidence(fresh=True)` OR `processes.orchestrator_process_alive()` (argv has /fa-orchestrator, cwd read only for candidates). Note throttle file `orch-silent-note.json`, event kind `orch-silent`.
- Spawn record gets `pid`/`start_time` only if `subprocess.run` result has an int `pid` (the acceptance fake sets it; real CompletedProcess has none).
- Skill-reading acceptance files (10) compared before/after the SKILL.md edit: identical PASSED/FAILED sets (27 passed).
- Editing processes.py makes the runner run tests/test_processes.py. Its `test_large_prompt_reaches_slow_reader_without_blocking_identity_recording` failed whenever run_logged took >1s: the observer thread calls `observe_tree(..., shared=, not_before=)` but the fake took only `(identities, roots)`. It also failed 2 of 3 runs on the base source. Fixed by matching the fake's signature; it then passed 3 of 3 runs.
- Adding an autouse fixture to tests/conftest.py: also run every test file that imports psutil. `test_resource_facts.py`'s 5 failures are pre-existing (they fail with the base conftest).
- Path("").resolve() is the CWD: reject empty/relative process paths before resolve() (review #590: missing cwd matched the gate's root).
