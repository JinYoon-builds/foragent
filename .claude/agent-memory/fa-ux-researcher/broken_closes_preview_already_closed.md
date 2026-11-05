---
name: broken-closes-preview-already-closed
description: Broken flow — fa order publish --dry-run previews closing an issue that is already closed (plan field closes)
metadata:
  type: project
---
Goal: preview a board-cleanup plan before approving closes.
Start: fa 0.1.0 (b106a151), repo /Users/apple/Dev/foragent; #1791 is CLOSED.
Shortest repro: plan.json with "issues":[] and "closes":[{"number":1791,"commit":"96b7dfd5"}]; run `fa order publish --dry-run plan.json`.
Observed: rc=0, prints "완료로 닫을 이슈: #1791 (해결 커밋: 96b7dfd5)." — no note that it is already closed. Preview also mixes English and Korean lines.
Reported 2026-11-05 via fa orch request, UNCONFIRMED (needs a second session). Check `fa orch inbox` before reporting again.
**How to apply:** rerun the dry run on the next visit; delete this entry once the preview says the issue is already closed. Related: [[broken-publish-help-closes]].
