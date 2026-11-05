---
name: broken-publish-help-closes
description: Broken flow — fa order publish --help schema omits the plan field `closes` (board cleanup) while saying unknown fields are rejected
metadata:
  type: project
---
Goal: write a board-cleanup PLAN.json that closes resolved or duplicate issues (order #1790 / #1791).
Start: fa 0.1.0 (14c7afc3).
Shortest repro: run `fa order publish --help`. The schema block lists `replaces` but not `closes` ({number, commit} / {number, into}).
The field works: a `--dry-run` with closes on /Users/apple/Dev/foragent previews the closes (rc=0). Also seen: the dry run previews closes for issues that are already closed.
Reported 2026-11-05 via fa orch request (status: reported, not yet confirmed). Check `fa orch inbox` before you report it again.
**How to apply:** rerun the help on the next visit. Delete this entry once the help documents `closes`.
