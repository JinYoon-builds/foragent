# Role signal raise (2026-10-27)

Jin approved: the gate computes each role's signals every tick from local state and raises a met signal as one orchestrator inbox request. The orchestrator answers each: call the role, or not now with a reason. Cooldown until the signal clears or 12 hours pass. Never escalates to the person; unanswered signals go to the retrospective. Raised, called, not-now and unanswered counts go to the scorecard so thresholds can be tuned from evidence. The gate never calls a role by itself.
