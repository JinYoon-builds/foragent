# watch-never-silent

Root cause (2026-10-23): `fa orch watch` raised on a failed read, wrote the error to stderr and exited. A Monitor listens on stdout, so blockers went unseen for 20+ minutes until the orchestrator-timeout escalation. #138 removes GitHub from the read path; this issue covers any read failure that remains.

Behaviour:
- A failed watched-state read prints one JSON line `{"unknown": true, "error": "..."}` on stdout. It never prints empty `blocked`/`needs_human`/`merged` sets for an unknown state.
- `--follow` keeps watching after a failed read, at the normal interval, and prints the real snapshot when reads work again.
- One-shot watch prints the unknown line and exits non-zero.
- An unknown reading does not overwrite the persisted last-known snapshot (`orch-watch.json`). The first good read after an unknown one prints even if it equals the last known snapshot.
- Update skills/fa-orchestrator/SKILL.md: an `unknown` line means the watcher could not read state; check `fa orch log`, do not treat it as an empty queue.
