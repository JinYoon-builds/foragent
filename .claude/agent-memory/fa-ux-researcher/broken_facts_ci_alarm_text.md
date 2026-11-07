---
name: broken-facts-ci-alarm-text
description: Plain fa orch facts lists ci_job_near_limit/ci_job_rising kind names only; no job name, durations or limit (confirmed 2026-11-07)
metadata:
  type: project
---
Goal: an operator learns from plain `fa orch facts` which CI job is close to its limit (#2497 promise).
Start: main 238c2f27, after order #2495 closed.
Repro: `fa orch facts` (takes about 2 min). Search for "acceptance-base": no hit. Only the line "kinds: ..., ci_job_near_limit, ci_job_rising" appears. The details are only in `--json` kind_numbers.
Observed: acceptance-base was at 93% of 45 min and reached the limit in 4 of 6 runs, but the text does not show it. Reported to the orchestrator inbox 2026-11-07, confirmed by a second session; reproduced again 2026-11-07 10:22Z and re-reported.
Recheck: when the plain text names the job, the durations and the limit, remove this entry.
