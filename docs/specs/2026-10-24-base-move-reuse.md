# A base move that cannot change the result needs no re-verification

Evidence (2026-10-24): #237 passed every check, then timed out with "base kept moving": main got 11 commits during its 70-minute run, 6 of them docs/ideas/decisions or order commits, and each move re-ran verification (run_publish.py refresh_base, limit 3 merges). With checks now taking about a minute, base churn became the binding constraint.

- When every main commit since the verified base touches only `docs/` paths or locked acceptance tests that the baseline excludes (approved-but-unmerged issues), the runner merges main without re-running verification, and that merge does not count toward the base-moved limit; the log says so.
- Anything else re-verifies as today: other src/ or tests/ paths, config files (.foragent.yml, pyproject.toml), locked tests of merged issues, and any file the PR itself changed. A mixed batch re-verifies once.

Facts only; no judgement about which code changes matter. Jin via planning (D83: a fully passing run timing out is a blocker).
