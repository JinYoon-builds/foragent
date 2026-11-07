---
name: broken-decision-remaining-ignored
description: decision-remaining ledger line silently ignored for decisions with several linked issues (D60 stays done) — CONFIRMED by second session 2026-11-07
metadata:
  type: project
---
Goal: record work an issue left, so `fa decisions` keeps the decision "in progress" (order #2355 / PR #2387).
Start: copy of the repo at main c05fea75; append to docs/decisions.md
`<!-- foragent:decision-remaining {"id": "D60", "issue": 1030, "what": "step 2"} -->`, run `fa decisions --repo-root <copy>`.
Observed: D60 stays `done`, no `remaining` in --json, no warning. Same line on single-issue D24 (#210) works (in progress + 남은 일).
Cause visible to user: any other linked (old, closed) issue counts as taking over the remainder.
Status: CONFIRMED 2026-11-07 (second session, exit 0, no stderr); re-reported via fa orch request. Owner: decisions lane, via orchestrator. Recheck after a fix; delete when D60 case works.
