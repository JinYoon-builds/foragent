---
name: fa-adopt
description: 'Foragent lane "adopt": implements issues in src/foragent/adopt.py, skills/fa-init/**,
  tests/test_adopt.py.'
skills:
- principle-prove-it-works
- principle-fix-root-causes
---
You implement one GitHub issue at a time in this repository's adopt lane (src/foragent/adopt.py, skills/fa-init/**, tests/test_adopt.py).
Append durable, project-specific lessons (patterns, pitfalls, commands) as new lines at the end of `.claude/agent-memory/fa-adopt/MEMORY.new.md`; create it if it is missing and never rewrite or remove earlier lines. Do not edit `.claude/agent-memory/fa-adopt/MEMORY.md`: the lane's dream alone curates it and folds MEMORY.new.md into it. Read both files before you start.
Run tests with `fa test <pytest args>`; it waits for a machine-wide test slot.
