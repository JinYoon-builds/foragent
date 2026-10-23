# Unfinished work survives a stop

Evidence (2026-10-23): when a run stops without merging (spec-question, timeout, check timeout, killed, stalled, retries-exceeded), `run_issue` removes the worktree and the implementer's uncommitted changes are lost. #163's branch held no commit of its work after several runs; the worker re-implemented from scratch. Orchestrator hints said "continue from the branch" and #236 resumes the worker session, but the code under it was gone. Every timeout was a cliff that threw away all work.

- Reuse the existing check-failure path (`preserve_failed_implementation` in runner.py) for every unmerged stop: commit the implementer's changes to `foragent/issue-<n>` as one WIP commit whose message names the stop reason, excluding .foragent/ runner files and restoring locked acceptance files to the approved version first, and push it when the run normally pushes.
- The next run of the issue starts from that branch, so the files are present before the implementer is called.
- No changes, no commit. A merged run still squashes as today.
