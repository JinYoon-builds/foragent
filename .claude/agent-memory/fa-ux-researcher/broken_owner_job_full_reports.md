---
name: broken-owner-job-full-reports
description: After #1957 (fbeaf856 installed), merge-owner job files still hold full comparison.reports (~1.8 MB each, folder 194 MB) (CONFIRMED 2026-11-07)
metadata:
  type: project
---
Goal: Jin checks that owner job files stop copying full acceptance reports (#1957 promise).
Start: ~/.local/share/foragent/JinYoon-builds__foragent/merge-owner, installed fa contains merge 3761d8d5.
Repro: ls -lt the folder; python3 -I prints key sizes of the newest job json. comparison.reports is ~1.79 MB (full GitHub run objects and base/head reports). Case: 2552.json written 17:32Z on 2026-11-07, 9 min after the fixed install. Merged jobs (1957.json, 2603.json) also stay on disk at full size.
Status: reproduced again 2026-11-07 17:52Z (same sizes, folder 164 files 194 MB) and re-reported with fa orch request (saved "reported"). Owner: orchestrator. Remove this entry when a newly written job file is under ~100 KB.
