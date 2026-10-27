---
name: project-650-wake-grace
description: #650 wake grace lives in sleep_credit.py beside #415's baseline; per-run activity check in scheduler_recovery stall branch
metadata:
  type: project
---

- `sleep_credit.credit()` writes `<state>/wake-grace.json` (`wake_at`=tick now, `until`=+300 s) in the same state lock when the increment > 60 s. Short sleep writes nothing, so it cannot extend or restart a grace.
- `sleep_credit.in_wake_grace(store, run, now)`: grace holds until `until` unless that run's `last_activity_at`/`last_progress_at` is newer than `wake_at`. Only the stall branch of `_recover_runs` checks it; runtime limit is unchanged.
- Report line reuses the existing `sleep_wake` key in `messages/sleep.py` (added by #415 but unused until #650), via `scheduler.text`.
- conftest stubs `capacity.slept_seconds` to 0 except in test_machine_sleep/test_sleep_credit; acceptance `sleep_case` re-patches it, so other modules never see a grace.

**Why:** the next sleep/stall issue should extend these, not add a new ledger file.
**How to apply:** tests `tests/acceptance/test_wake_grace.py`, `test_machine_sleep.py`, `tests/test_sleep_credit.py` (all fast, ~5 s).
- Unit tests that reuse acceptance helpers (`sleep_case`, `tick_case`) need `monkeypatch.syspath_prepend(tests/acceptance)`; the directory is on the path only when an acceptance module is collected. The wake-line test in `tests/test_sleep_credit.py` does this.
- Break-it is done by `sed` mutation plus `git show HEAD:<file> > <file>` to restore. Copying to /tmp and `git checkout` are both blocked here.
- Baseline regression in test_orch_facts (2026-10-27) was not reproducible on the branch; facts reads sibling state roots and the live load sensor, so it can flake under machine activity. Answered with REBUTTAL.
- Review (state/report deadline drift): the wake line must print the stored `until` from `wake_grace(store)` as HH:MM:SS, never recompute from a second `ctx.now()`. The test fakes a 2 s delay in credit() just before a minute boundary to prove this.
