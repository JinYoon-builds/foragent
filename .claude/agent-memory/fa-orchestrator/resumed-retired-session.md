---
name: resumed-retired-session
description: alive=false while you hold the seat → FORAGENT_SESSION_ID differs from the heartbeat's active id (inherited on refresh or resumed retired session)
metadata:
  type: feedback
---
After you start `fa orch watch --follow`, run `fa orch alive --json`. It must show alive=true within a minute. If it shows false while `fa orch seat` names your process, compare `$FORAGENT_SESSION_ID` with `session_id` in `<state>/orch-heartbeat.json`. write_heartbeat ignores renewals from any id that is not the active one.

**Why:** On 11-07 two cases happened. (1) The retired session bdc6c5e3 was resumed and took the seat. (2) The fresh session opened by `fa orch refresh` (pid 31775) inherited the parent's FORAGENT_SESSION_ID=bdc6c5e3, while the heartbeat named 1ae2fd18 as active. In both cases alive=false lasted for hours, and the PM noticed first.

**How to apply:** If you are a resumed retired session and an active orchestrator session exists, write the handoff (`fa orch handoff --file`, every section required), stop your watch and SendMessage the active one. If you are the fresh session with an inherited id, restart the watch with `FORAGENT_SESSION_ID=<heartbeat session_id>` and order the spawn fix. Do not decide that you are retired from the env id alone: it may be inherited.
