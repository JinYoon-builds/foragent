# Tell the worker who runs which tests

Evidence (2026-10-23): runner.py:216 tells the implementer "Optional additional check: before you finish, also run the full test suite". #138's worker then ran unrelated tests and parallel helper scripts and spent its whole 90-minute budget; the same tests ran three times (worker, runner, CI). Quality stays guarded by the runner's checks, cross review and CI.

Replace the invitation with the actual situation (a description, not a prohibition): the runner runs the Done-when tests, the acceptance tests that already passed, and related unit tests; CI runs the full suite after publication; the worker runs only the Done-when tests and the tests of the files it changed, and needs no parallel test helper.
