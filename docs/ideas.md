# Ideas parking lot

Ideas Jin wants to keep but not order yet. An idea moves to `decisions.md` when Jin decides it, and to an order after that. Newest first.

## 2026-10-24

- **Docs that explain Foragent through operating-system concepts (for the open-source release).** One page per element, mapped to the OS idea it mirrors: policy vs mechanism (orchestrator plan vs gate, D80), the scheduler and run queues (lanes, including the priority inversion behind #138), AIMD parallelism as TCP congestion control (#106), test slots as semaphores (#243), lanes as mutual exclusion by file ownership, plan expiry as a lease, crash/stall recovery as process supervision. Each page also says where the analogy breaks: the "processes" judge and can be wrong, and the scarce resources are token quotas and API limits rather than CPU. Back the claims with scorecard numbers (merges per hour, regressions caught). Write after the core is stable.

## 2026-10-23

- **Lane workers using the harness's own multi-agent features.** A Claude worker could fan out subagents inside its run (investigate, implement, test in parallel) and use the idle Claude quota. Risks seen the same day: more processes and machine contention, orphaned background shells (#184), subagents editing the same worktree at once (the conflict lanes exist to prevent), and runs the gate and scorecard cannot see. Revisit after #106 (machine budget), #243 (test slots) and #261 (orphan cleanup). Open read-only helpers (investigation, review) first, for large issues only.
- **Heavy checks on GitHub Actions for public repositories.** Actions minutes are free for public repos and can shard the suite across runners. Keep the local machine as the default (users' repos may be private and have limited minutes); offer Actions as an opt-in, and avoid polling GitHub for results (the API limit). Do after reducing repeated test runs (#272, #257, #243).
- **A local-model worker.** When local models are good enough, easy issues could go to a local model for free and hard ones to Claude or Codex; one more choice for D74's executor selection.
- **Faster unit tests.** Many tests build a temporary git repository and run programs, about 0.2-0.7 s each. Reusing fixtures could make the suite several times faster (a D68 tidy task).
- **Narrower related-test selection.** Changing a central module selects about 1,900 tests. Coverage-based selection would cut this but can miss breakages; a trade-off for Jin after the scorecard (D77) shows check-phase time.
- **Time budget by issue size.** Every run gets 90 minutes; large issues could get more. Decide from the scorecard's per-size times.
