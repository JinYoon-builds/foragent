# A finished stop clears the running label

Evidence (2026-10-23): after #248, #184, #232, #178 and #210 ended without merging, a second history row `crashed` ("Crash recovery for running label generation N") followed and crash-restart put the issue back to `ready` with no orchestrator decision. Cause found in code: `run_issue`'s `SystemExit` path (the gate's SIGTERM on a time limit or stall) records the finished run but does not remove the `running` label, so the next tick treats it as an orphan.

- Every way a run ends without merging, including killed and stalled, removes `running` before the runner exits and keeps `blocked`; the local snapshot (#138) follows.
- The next ticks write no `crashed` row for it and do not return it to `ready`; the orchestrator decides.
- A genuinely dead runner (record says running, pid gone) is still recovered as today.
