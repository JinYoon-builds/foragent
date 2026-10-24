# D81: with CI, the runner runs only the minimum

See docs/decisions.md D81. Overnight 2026-10-23/24 most gate failures were the runner's related-unit-test check timing out (about 1,900 tests for a central file; 15-36 min on the Mac mini under contention, about 11 min alone; #163 took 36 min even after #243), while GitHub CI runs the same unit suite on every PR and merge in 2-3 minutes.

1. With CI (a workflow under `.github/workflows/` with a `pull_request` trigger, detected as a fact): the runner runs the Done-when commands, the acceptance baseline protection, and the test files of the files the change touched (a changed `src/foragent/x.py` -> `tests/test_x.py`; a changed test file itself). Other unit tests are left to CI; a red CI still goes back to the worker (resume #236, kept work #270).
2. Without CI: run all related unit tests as today.
3. Team setting `runner_unit_tests: auto | ci | all` in .foragent.yml (default `auto` = 1/2); other values fail config loading. Shown and editable in the lane view settings screen (D70 `s`) as "Unit tests the runner runs: leave to CI / run all".
4. Baseline protection and green CI before merge stay in every setting.
