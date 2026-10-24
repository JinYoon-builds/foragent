# Facts show related-test selection (edit to #333)

Evidence (2026-10-24): the related-unit-test step selected tests/test_runner.py (505 tests, 22-34 min) for #274/#184, and for #196 it picked files of another merge because the base moved (#344). Only the raw run log showed this.

- The runner records each related check on the run (`Run.related_tests`): changed-file count, selected files, seconds per file (JUnit), total seconds, timed out. A timeout keeps the selection and total.
- `fa orch facts --json` shows per run the related checks and the test-phase share of runtime; across recent runs the slowest test files with how many runs selected each; the human view names the slowest file and its share in one line. A run with no related check shows an empty selection.
- D82 addenda 1 and 3: these are numbers for the orchestrator and architect, not a splitting rule.
