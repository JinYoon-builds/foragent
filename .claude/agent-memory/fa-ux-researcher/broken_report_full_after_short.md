---
name: broken-report-full-after-short
description: Unconfirmed claim that fa brief --report --full run right after --report cannot show the merges the short report counted
metadata:
  type: project
---

Goal: read the three-line /fa report, then see the details of what it counted.
Start: any clone of the foragent repo.
Claimed repro (earlier session, fa 4d11da79): `fa brief --report` says "머지 N개" (N>0), then `fa brief --report --full` counts from the time the short report just recorded, so it shows fewer merges or "끝난 일은 없어요". `fa brief` and `--json` have no merge list (still true on 506c471a).
Status 2026-11-06: NOT in fa orch inbox (the earlier "reported" note was wrong). Recheck on 506c471a did not reproduce: three short→full pairs all said 머지 0개 because no merge landed between reports. To test it, a merge must land after the previous report and before the short one; poll with `gh pr list --state merged --search "merged:>=<last report time>"`.
Oddity explained 2026-11-06: 머지 counts closed issues, not PRs. #2288's issue #2292 closed 07:13:40Z, after that report. Hand PRs without an issue (#2349 at 12:44:03Z) count as 0. So to test, wait for an issue to close (gh issue list --state closed --search closed:>=...), not a PR merge.
