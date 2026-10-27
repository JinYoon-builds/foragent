# Request box and question routing

Every request or question sent as a cross-session message is also written to the box, even when the
message itself arrives, so nothing is lost on restart.

The planning session runs `fa plan heartbeat` and counts as alive for 120 seconds. If it is alive,
also send the question to it as a cross-session message; `fa orch ask` then skips the push. If it is
absent or its heartbeat is stale, `fa orch ask` sends the push itself, so do not add `fa notify`.
