---
name: project-333-facts
description: #333 fa orch facts (orch_facts.py) and Run.related_tests recording — read-only rules, junit flag form, sensor observing mode
metadata:
  type: project
---

- `fa orch facts` lives in `orch_facts.py`; CLI wiring is in `commands/orch.py` (`INBOX_ACTIONS` includes "facts", so no Git/GhTracker setup). Acceptance tests snapshot every file under tmp_path (bytes + mtime), forbid Popen/kill and guard `open(plan.json)`.
- `capacity.machine_load()` writes a sensor file and lock under `machine_root()` (tmp_path in tests). Facts wraps it in `capacity.observing()` (ContextVar) so the real sensor is read-only, while a test's patched `machine_load` is still called.
- Do not use `capacity.machine_runs` in read-only views: it probes sibling runs with `os.kill(pid, 0)`.
- Related unit checks pass `--junitxml <path>` (space form). Locked `test_fast_checks` counts `--junitxml=` commands as the single baseline discovery. Report path `<state>/runs/issue-N-related.xml` is unlinked before and after each check so a timeout never reuses old timings.
- `_check(..., clock=ctx.monotonic)` measures only the command inside the test slot (`CheckResult.seconds`).
- Phase events (`kind: phase` in orch-events.jsonl) are assigned to the latest execution of the issue started at or before the event `at`. Waste = phases ending in `-wait` plus events with `repeat_of`, each counted once.
- Test-slot `current` = shared `test-slots.json` capped by config `test_slots` (the locked test demands 4 with no machine.yml). What the runner really admits is `capacity.effective_test_slots()` (machine.yml default 2 applies), reported as `resources.test_slots.admitted`.
- Review lesson: facts must replay the gate's own admission, not re-derive it. `overlap_files.admits()` (split from `can_start`) and `claims(measured=False)` give the read-only file rule. `scheduler_start._lane_held` gives quota holds. The machine cap (capacity.json, capped by machine.yml) is compared with machine-wide runs; the project's `max_parallel` only with this project's runs. Locked keys: `test_slots` must be exactly current/queue_length/total_wait_seconds, so extra fields like `admitted` go under `resources`.
- Merges since tidy: `fa tidy done` → `tidy.status()` count; a tidy-done event → `merge_times()` first, then `finished_at`.
- Review round 2: a consumer that passes on seeded fixtures is not enough. Reviewers check that production code emits what facts reads. Phase events are now written by `StateStore.update_run` (on phase change) and `finish_run` (#224 schema, under the state lock). `_test_slot` sets `test-slot-wait`; baseline capture sets `baseline`/`baseline-wait`; `Run.phase_work_id` + `work_events` (carried by start_run) produce `event_id`/`work_id`/`repeat_of`. `orch.log` skips kind=phase.
- Replay parity: lanes via `issue_lane(labels, config.lanes)`, order via `orch_queue.ordered`, union via the gate's recorded `union-merge-paths.json` (`overlap_files.record_union`). An empty repo root makes `_filesystem_case_sensitive` return None, so every pattern overlaps; give unit-test roots a file.
