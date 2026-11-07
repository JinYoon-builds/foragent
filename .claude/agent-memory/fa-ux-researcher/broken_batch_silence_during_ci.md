---
name: broken-batch-silence-during-ci
description: merge batch retired "after 60 minutes of silence" while tip CI was finishing jobs and green (CONFIRMED 2026-11-07, after #2172)
metadata:
  type: project
---
Goal: the owner learns why a merge batch was dropped.
Start: ~/.local/share/foragent/JinYoon-builds__foragent/merge-owner.log plus merge-batch/<tip sha>.json.
Repro: grep "minutes of silence" in the log -> tip sha. In the batch json, last_progress equals the build time and ci_status is null. Use gh api commits/<sha>/check-runs to list the jobs: they finished all through the window, a workflow went green 18 minutes before the retirement, and the jobs still running were cancelled at the retirement. Case: dc72ce123fdd, built 11:11Z, retired about 12:19Z on 2026-11-07.
Status: reported 2026-11-07 and re-confirmed by a second session the same day (saved as 'reported'; not yet visible in fa orch inbox). Recheck the next batch whose tip CI runs longer than 60 minutes. Remove this entry when CI progress moves last_progress.
