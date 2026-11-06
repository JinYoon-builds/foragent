---
name: broken-report-full-after-short
description: fa brief --report --full run right after --report cannot show the merges the short report counted
metadata:
  type: project
---

Goal: read the three-line /fa report, then see the details of what it counted.
Start: any clone of the foragent repo, fa 0.1.0 (4d11da79), 2026-11-06.
Shortest repro: `fa brief --report` (line 1 says "머지 N개"), then `fa brief --report --full`. The full report counts from the time the short report just recorded, so it shows fewer merges or "끝난 일은 없어요". `fa brief` and `--json` have no merge list.
Observed: the counted merges cannot be seen. Each run takes about 40 s and moves the report time forward, and that time may be shared with the person's real checkout.
Reported 2026-11-06 via fa orch request (confirmed by a second session). Re-test after a fix and remove this entry once it works.
