---
name: edit-plan-dropped-done-when
description: drafts/bin/edit_plan.py silently dropped Done when commands and checklist lines until 2026-11-01; check any issue edit's old and new body
metadata:
  type: project
---

Until 2026-11-01 `drafts/bin/edit_plan.py` read only the first sh block of Done when and looked for a Checklist section that issue bodies do not have, so an order edit lost later commands and every checklist line. It is fixed, and the plan builder now keeps them.

**Why:** found while editing #1511, where it would have dropped 2 commands and 15 checklist lines. Edits since 10-30 may have lost lines: open #1004, #997, #1007, #994, #1482. Some could be deliberate; the history does not say which.

**How to apply:** after building an edit, compare the plan's done_when and checklist counts with the current issue body. To audit past edits, read GraphQL `userContentEdits` for the issue.

Since #1556 merged (2026-11-01), `fa order publish` and preread refuse an unexplained removal, and `fa order edit N --json` starts an edit from the issue's current sections. Use that and stop using the helper.
