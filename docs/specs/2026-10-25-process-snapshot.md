# One process-table read per tick

Evidence (2026-10-25 ~06:50 KST): gate ticks took 10+ minutes each (kick.log: 171 "another tick is running" skips). A read-only `sample` of a tick showed it CPU-bound in Python on __sysctl/sysctl with heavy object churn: process-table reads by the #261 stop code (orphan reap over idle worktrees, identity verification). Recorded identities per run reached hundreds or thousands (#105: 377, #376: 1869). Slow ticks delay every start and merge (D88: waiting).

- A tick reads the process table at most once and checks every recorded identity and orphan candidate against that snapshot.
- Each run's recorded identities are pruned to those still alive in the snapshot; an identity whose liveness could not be read is never pruned.
- #261's stop guarantees hold unchanged (verified members of recorded trees only; start-time check; no record, no signal).
- tick-outcome.json records duration_seconds; fa gate status shows it.
