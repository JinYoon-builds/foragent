---
name: issue-106-capacity-aimd
description: #106 machine-wide AIMD run budget — where state lives, the locked-test conflict with load_config's missing-file guard, and scheduler test pinning
metadata:
  type: project
---

#106 (D60 1) adds `src/foragent/capacity.py` (next_parallel, machine_load, machine_runs, machine_lock, capacity.json/capacity.lock under `store.root.parent` = ~/.local/share/foragent). scheduler imports the names so tests can patch `scheduler.machine_load`/`scheduler.next_parallel`.

**Why:** the locked acceptance test patches those scheduler globals and checks a file directly under the data root changes.

**How to apply:**
- Machine root is `store.root.parent`, not `Path.home()`, so unit tests with `StateStore(tmp_path/"state")` stay isolated from real live runs.
- `tests/test_scheduler.py` ctx fixture pins `machine_load` to ("normal", 0.0); otherwise real load/pressure makes scheduler tests flaky.
- The locked test once called `load_config` on an empty folder; it was fixed upstream (4af3569) to write `.foragent.yml` first. Keep the "not a Foragent repo" guard.
- Review fixes: `max_parallel` is a per-repository ceiling (own active runs), never a machine cap. The stored AIMD value is only pressure/load-driven (`lanes_with_work=max(demand, current)`); the lanes cap applies to the tick budget only. Pid-less runs and `run-starts` reservations from other repos hold a slot only within `LAUNCH_GRACE` (2 min, shared with `_recover_runs`). The machine lock covers only recount + `prepare_run_start`; GitHub calls happen outside.
- Full unit suite takes ~620s, over the 600s foreground limit; split it into two foreground halves instead of a background run.

- #400: 레인 2개 상한(`_lane_has_room`)을 없앴다. `_can_start_in_lane(ctx, lane, issue)`는 레인의 실행 중인 모든 이슈의 승인 스냅샷 파일과 겹침을 검사하고, 어느 쪽이든 files가 비면 시작하지 않는다. `_start_ready`는 시작 뒤 같은 레인을 계속 돌며 `_has_slot`이 찰 때 멈춘다. 예산 수요는 일시정지 안 된 레인의 ready 이슈 수 전부(실행 중인 레인 포함)다.

- #991: `capacity.hold_workload(store, config, *, tests, wait_s, kind, issue, role)` writes `<store>/workloads/<pid>-<hex>.json` (`capacity_sessions.py`, owner-only writer) after admission under `machine_lock`. `machine_holders()` labels every slot ("<project> #N" / "<project> <kind>") and `machine_runs = len(machine_holders)`, so counting and wait messages never diverge. Live pid via `_alive` (os.kill 0); an active run absorbs ONE workload with its pid (runner's own agent), extra ones (N reviewers) count. Nesting reuses via ContextVar; a contextmanager generator kept without a reference is GC-closed and releases its record (test pitfall). Admission reads sensors under `observing()` so it never moves the gate's baselines; wait_s=0 raises TimeoutError silently.
- #991 review: admission uses `machine_holders(live=True)` for every store (gate's `machine_runs` keeps own-dead runs; its recovery owns them). A run absorbs its own agent's workload by pid, or by issue while pid-less/only a `run-starts` reservation; admission compares holder counts with/without a `candidate` workload and admits a reuse regardless of budget/sensors. One monotonic deadline covers machine lock (single LOCK_NB try per loop so every wait prints), guard lock (`_take_test_slot(limit, deadline)` via `_flock_until`) and test slot (`hold_test_slot(deadline=, timed_out=)`). macOS has no `timeout` binary: mutation runs need Python `subprocess.run(timeout=)`. Base check: `git archive HEAD src` into `.foragent/base` + `PYTHONPATH`.
- #991 review 2: run_review runs reviewers via `copy_context().run` in pool threads, so ContextVar values leak into concurrent agents. Nested reuse of `_WORKLOAD`/`_WORKLOAD_TESTS` is tied to `threading.current_thread()` (Thread object, not ident) so only the holding thread nests.
