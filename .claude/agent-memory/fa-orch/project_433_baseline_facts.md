---
name: project-433-baseline-facts
description: #433 baseline build facts — run_baseline_limits.py shared limit, facts membership rule, timed-out entry conflict with locked prebuild test, zsh test-list pitfall
metadata:
  type: project
---

- Shared pure limit/membership/trend code lives in `src/foragent/run_baseline_limits.py`; `run_baseline._baseline_capture_limit` only logs. `load_reusable()` is the one reuse gate that skips `timed_out: true` entries (runner hit, premerge, prebuild).
- Membership: `repository` path decides; without it the `repo` slug; without either a companion `.xml` (legacy).
- `fa orch facts` reads the cache in place from `orch_facts_baselines.collect()` after the freeze; the human line goes at the end of the last block (test_orch_facts requires exactly 5 blocks).
- Timed-out captures are written (`record_timeout`) in the runner/premerge path only; the background prebuild passes `record_timeouts=False` because locked `test_baseline_prebuild.py:336 [timeout]` asserts no `*.json`. The reviewer rejected skipping the write everywhere, so rebut only the exact instance a locked test forbids.
**Why:** future baseline-cache issues hit the same split.
**How to apply:** keep the prebuild exception until that locked test changes.
- The detached builder (`run_baseline_process.main`) rebuilds Config from the request: any new `ctx.config` read on the `_build_baseline` path must be added to `start_baseline_process`'s request.
- Companion-XML duration estimates apply to every entry without `duration_seconds`, path-tagged or not (old runner behaviour).
- zsh pitfall: `fa test $T` with a space-separated list passes one argument; use `${=T}`. An empty FAILED-ID diff may just mean pytest found no file.
