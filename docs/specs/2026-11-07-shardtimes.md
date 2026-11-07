# Acceptance shard times stay fresh (2026-11-07)

Architect r988: scripts/shard_times.json has no writer; 46% of acceptance files are untimed, so shard 0 timed out twice and retired two trains. This order refreshes it and gives it one writer.

PM r990: also alarm when a CI job nears its time limit or trends up, so slow drift is caught before a cut fires.
