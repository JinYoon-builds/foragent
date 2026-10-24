# The tick builds the acceptance baseline once per base commit, ahead of the runs

Evidence (2026-10-24): each run built its own acceptance baseline at start (about 700 tests, 5-6 min alone). With six runs at once the build passed its 900 s limit and ended `count=0` in 26 issues' logs, so those runs went on without passing-test protection. The cache (#257) only helps a second run on the same base; a failed build is not cached.

- When a tick sees a main SHA with no cached baseline, it starts one background build for it, holding one test slot (#243), and records `kind=baseline-build` with the SHA; no second build for the same key while one is in progress or done.
- Runs read the cached baseline and never run the baseline pytest themselves; while the build is in progress they wait, and the wait is charged to neither the baseline limit nor the run budget (waited_seconds).
- A failed or timed-out prebuild is not cached and a later tick retries it, at most once per key per tick.
- When main moves, the next tick builds for the new SHA; runs started on the old SHA keep the old cached baseline.
Related: the outside fix (A) that excludes other unmerged issues' locked tests from the baseline and refuses to merge a run whose baseline could not be built.
