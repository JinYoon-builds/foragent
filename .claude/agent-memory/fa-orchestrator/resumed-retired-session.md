---
name: resumed-retired-session
description: fa orch alive says not running while my watch runs — gate-spawned session from claude bg-spare keeps a retired FORAGENT_SESSION_ID; restart watch with the heartbeat's active id
metadata:
  type: project
---

`fa orch alive` = "not running" although `fa orch watch --follow` runs. `write_heartbeat` drops writes whose
`FORAGENT_SESSION_ID` differs from the active `session_id` in `orch-heartbeat.json`. Gate spawns go through a
pre-started `claude bg-spare` process: it keeps an old env id and its own Claude id (transcript is
`$CLAUDE_CODE_SESSION_ID.jsonl`, not the recorded one). Seen 11-08 15:17 (50fc6b28) and 16:18 (82fd3163); fix ordered #2861.

**Why:** stale heartbeat → gate logs "alive without heartbeat", silence notices, refresh-due never finds the transcript.
**How to apply:** after starting the watch run `fa orch alive`. If false and `orch-spawn.json` spawned_at equals the
heartbeat's `started_at`, that active id is me: rerun the Monitor as `FORAGENT_SESSION_ID=<active id> fa orch watch --follow`.

**Fixed by #2861 (merged 11-09 08:08 KST, verified live):** a plain `fa orch watch --follow` on a tool that includes
#2861 records the session's real Claude id; `fa orch alive` and `fa orch refresh-due` both work. Use the manual
FORAGENT_SESSION_ID override only on a tool older than feffc3c4.
