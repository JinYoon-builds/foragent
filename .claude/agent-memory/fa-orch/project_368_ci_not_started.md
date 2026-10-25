---
name: project-368-ci-not-started
description: How CI jobs that never started are detected (tracker CIEvidence) and held (run_publish ci-not-started) without a worker round
metadata:
  type: project
---

#368: `GhTracker.pr_failed_logs()` returns `CIEvidence` (a `str` subclass with `.not_started`). Per failed run it reads `actions/runs/<id>/jobs` (`--jq .jobs`) and each failed job's `<check_run_url>/annotations`. A job "never started" if it has 0 steps or an annotation matching "job was not started". Logs (`--log-failed`) are fetched only for runs with a started failed job or with no failed jobs (invalid workflow); "log not found" becomes evidence text, other errors still raise.

**Why:** the acceptance test only monkeypatches `pr_checks`/`pr_failed_logs` onto FakeTracker, so the classification must travel through the `pr_failed_logs` return value; FakeTracker plain strings keep `not_started` False via `getattr`.

**How to apply:** `run_publish._hold_if_not_started()` runs inside `_ci_failure()` (after the head check, before spending an attempt) and in the rebuilt-attempt branch; it flags `Reason.CI_NOT_STARTED` (group tech-failure) and raises `ExecutorStopped("held")`. review-only shares `_ci_failure`, so `fa orch rerun-review` re-holds or merges the same head. New reason codes must also be added to `tests/test_reasons.py::test_groups_cover_exact_contract_codes`.
