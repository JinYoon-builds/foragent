# Question routing

The planning session runs `fa plan heartbeat` and counts as alive for 120 seconds. If it is alive,
also send the question to it as a cross-session message; `fa orch ask` then skips the push. If it is
absent or its heartbeat is stale, `fa orch ask` sends the push itself, so do not add `fa notify`.
