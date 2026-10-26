# Acceptance-test corrections (2026-10-26)

This order only re-publishes corrected acceptance tests. Each issue's goal, files and work are unchanged; implement each issue from its own Goal as before.

What changed in the tests (already done by the order, part of the locked files):

- test_baseline_follows_base.py compares the created PRs' numbers with merged_prs. It used to compare PullRequest objects with numbers. FakeTracker needs no custom equality.
- test_progress_words.py: the two ETA tests keep their names but check only totals and that progress is a local observation. The finish-time rule is in test_eta_remaining_work.py (#503). #503 lists the file in its Done when.
