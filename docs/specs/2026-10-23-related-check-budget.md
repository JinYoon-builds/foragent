# The related-test check uses the run's remaining budget

Evidence (2026-10-23): runner.py:1255 runs the related unit tests with a fixed 900 s limit. #163 selected 1,417 tests; split runs all pass (622 in 404 s, 407 in 485 s) but the single command is killed at 900 s ("Check timed out.") on every attempt with no failing test. #184, #250 and #210 died the same way; machine contention only made it sooner.

- The related-unit-test check's limit is the run's remaining budget, never below the 900 s floor.
- A check killed by its limit ends the run as `timeout` with a detail naming the check, its limit and the number of selected tests; it is not sent back to the worker as an implementation failure, so attempts are not spent re-running the same check.
