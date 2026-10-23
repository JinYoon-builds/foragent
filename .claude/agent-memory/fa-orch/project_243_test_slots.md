---
name: issue-243-test-slots
description: #243 machine-wide test slots — why they live under Path.home() not store.root.parent, flock release, waited accounting, test isolation fixture
metadata:
  type: project
---

#243 adds machine-wide test slots to `src/foragent/capacity.py` (`hold_test_slot`, `update_test_slots`, `effective_test_slots`, `next_test_slots`; files `test-slots.json`, `test-slots.lock`, `test-slots/slot-N.lock`).

**Why:** the locked acceptance test builds two repos whose stores have *different* parents, so slots cannot use #106's `store.root.parent`; they use `capacity.machine_root()` = `Path.home()/.local/share/foragent` (same dir as `StateStore.for_repo` in production).

**How to apply:**
- `tests/conftest.py` autouse `isolated_test_slots` patches `capacity.machine_root` to `tmp_path/"machine-data"`; without it unit tests would queue behind live runs on this machine.
- A slot is a non-blocking flock on a slot file; the kernel frees it when the holder dies. Name context managers without a `test_` prefix (pytest would collect them when imported).
- Runner `_check(..., slot=_test_slot(n, ctx))` takes the slot inside `execute`, so timeouts precomputed by callers exclude queue time; `_test_slot` credits `waited_seconds` with `ctx.now()` deltas (the acceptance clock is fake; `ctx.sleep` advances it, so the slot loop uses real `time.sleep`).
- Tick samples `machine_load()` once per tick for both test slots and `_machine_budget`; test files that run tick through the CLI must pin `foragent.scheduler.machine_load` or the real sysctl call goes through patched `subprocess.Popen`.
- Review fixes: `fa test` is dispatched in `cli.main` before argparse (REMAINDER rejects leading `-q`/`-k`); only a leading `--repo-root` is ours, root otherwise = nearest `.foragent.yml` upward. A held slot passes `FORAGENT_TEST_SLOT=held` (`capacity.HELD_ENV`) to the child via `run_logged(env=...)`; `hold_test_slot` yields at once when it is set (nested `fa test` deadlock). Contended baseline-cache lock waits go through `_credited_wait` (shared with `_merge_lock`); uncontended path tries LOCK_NB first so no monotonic noise lands in `waited_seconds`.
