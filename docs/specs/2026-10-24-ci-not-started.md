# A CI job that never started is an infrastructure hold, not a code failure

Evidence (2026-10-24): GitHub Actions stopped starting jobs for billing. Run 40082146744: conclusion failure, job "test" with 0 steps, annotation "The job was not started because recent account payments have failed or your spending limit needs to be increased". `gh pr checks` reported fail; fetching failed logs returned "log not found: <job id>", so #163 and #197 ended as runner errors and retries-exceeded, and other runs would have sent workers to fix nothing.

- Every failed job never started (0 steps or the not-started annotation, including when its log is missing) → hold with reason `ci-not-started`, detail quoting the annotation and naming the run; no worker round, no CI fix attempt; PR stays open.
- A job that failed with steps still goes to the worker as today; mixed cases follow the real failure and mention both.
- The hold is not sticky: once CI runs and passes on the same head, the issue merges through the normal recheck path.
