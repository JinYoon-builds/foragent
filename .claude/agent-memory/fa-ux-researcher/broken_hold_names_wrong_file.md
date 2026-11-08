---
name: broken-hold-names-wrong-file
description: fa dashboard <N> wait line names an overlap file the blocker neither declares nor changes (seen after #2412, confirmed 2026-11-07)
metadata:
  type: project
---
Goal: learn why an issue waits and on which shared file.
Start: #2333 waiting behind #2332 (PR #2431 open).
Repro: `COLUMNS=100 fa dashboard 2333 --once` → "#2332 뒤에 해요 · 파일도 겹쳐요: src/foragent/decisions.py".
Failure: decisions.py is only in #2333's Files; #2332 neither declares nor commits it. Real overlap orch_dream_worker.py is not shown.
Reported via fa orch request 2026-11-07 (re-reproduced and re-sent same day, third session). Also overlap orch_architect.py; header shows lane globs, not issue Files. Retest while some issue is held on overlap; remove when the named file is a real shared file.
