# run-failure-reasons

Evidence (runs.jsonl, 2026-10-23 01:00-08:30 KST): 30 runs, 11 merged. After #195 (05:28) 8 new runs, 2 merged.

Root causes found in code:
1. `run_issue` and `review_only` catch an exception, append the traceback to the log and flag the issue on GitHub, but `finish_run` gets no `detail`. An `error` row in runs.jsonl says nothing (#178, #201, #164, #166, #191, #111, #116).
2. The tick start path writes GitHub first (`remove READY`, `add RUNNING`, `board.sync_status`) and the local run record after. When a GitHub write fails in between, including a write GitHub applied but whose reply was lost (REFUSED_STREAM after the body was written), the tick stops with a `running` label and no live run. The next tick treats it as an orphan and writes `crashed` with `started_at == finished_at` and `pid None` (#138 22:05, #163 21:01). Two such crashes in a row block the issue as retries-exceeded. This is not the retry/tick race seen on #178.
3. A rate-limit 403 during publish (`get_issue` in `publish_locked`) is caught by the generic handler and becomes `error` (#178, #201). #210 adds rate-limit waiting; this issue makes sure the publish and review stages use the same wait.

Behaviour:
- Every finished run that is not merged keeps a one-line reason (at most 500 chars) in `detail`, in the state record and in runs.jsonl.
- Starting a run records it locally before any GitHub write. The board status update is display only; its failure never stops a start. A GitHub failure before the launcher ran is not a crash and does not count toward retries-exceeded; the next tick starts the issue once.
- A rate-limit error at any runner stage (implement, publish, review) waits like #210 instead of failing.
