---
name: no-sendmessage-to-pm
description: Reach PM only through fa orch ask/request-status; SendMessage across permission modes wakes Jin on his phone
metadata:
  type: feedback
---

Do not SendMessage the PM (or any session in another permission mode). Use `fa orch ask`, `fa orch questions`, `fa orch request-status`; PM reads them about every 30 minutes.

**Why:** PM 11-07 r1091: the orchestrator and PM run in different permission modes, so each cross-session message is held for Jin's approval on his phone; one woke him at 22:36.

**How to apply:** Overrides [[peer-messages-in-korean]] and the skill line "send it to planning with SendMessage". Same for resumed retired orchestrator sessions ("prompting" mode).

Seat pitfall (11-07): a refreshed session inherits the parent's FORAGENT_SESSION_ID, so its heartbeat is ignored (alive=false) while it holds the seat. Fix in place: run the watch with `FORAGENT_SESSION_ID=<active id from orch-heartbeat.json>`; check `fa orch alive --json`.

Closed-lock alarms (11-07 r1075-77): "closed issue's locked test fails on main" can be a strict xfail from tests/merge_path_expected_red.py (old merge-turn path, I1/d,g,h). Run the named tests on clean main first; "3 xfailed" means no new break.
