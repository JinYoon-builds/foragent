---
name: broken-retired-main-write-invisible
description: After #2857, a retire is meant to be in fa orch log, but the log shows only the last 20 events (minutes); retire help still says "shown in status"
metadata:
  type: project
---
Goal: read the outcome and reason of a retired main-write request after later work lands (q93, #2857).
Start: /Users/apple/Dev/foragent, fa 4f0b486b or later. Request 106 was retired on 2026-11-08, before #2857.
Repro: `fa orch log | wc -l` gives 20, covering about 4–9 minutes. `--help` has only --repo-root, and `-n`/`--all` are rejected. `grep -i "retir|106"` finds nothing. Status main_writes is [] (by design). `retire --help` --why still says "shown in status", and `id` does not say that the sequence number is accepted.
Status: confirmed by a second session. Reported through fa orch request on 2026-11-09 (sent for confirmation; check fa orch inbox for the r-id). Owner: orchestrator. Next: after a fix merges, check that a retire can still be found at least 1 hour later. Remove this entry when it can.
