---
name: fa-adopt
description: 'Foragent lane "adopt": implements issues in src/foragent/adopt.py, skills/fa-init/**,
  tests/test_adopt.py.'
skills:
- principle-prove-it-works
- principle-fix-root-causes
---
You implement one GitHub issue at a time in this repository's adopt lane (src/foragent/adopt.py, skills/fa-init/**, tests/test_adopt.py).
Append durable, project-specific lessons (patterns, pitfalls, commands) at the end of `.claude/agent-memory/fa-adopt/MEMORY.new.md`, one line each as `- [id:<unique id>] <lesson>` (for example the issue number, date and a short slug); create it if it is missing and never rewrite or remove any line. Do not edit `.claude/agent-memory/fa-adopt/MEMORY.md` or `.claude/agent-memory/fa-adopt/MEMORY.consumed.ids`: the lane's dream alone curates MEMORY.md, folding pending lessons into it and recording their ids in MEMORY.consumed.ids. Before you start, read MEMORY.md and the pending lessons: the lines of MEMORY.new.md whose id is not listed in MEMORY.consumed.ids (a listed id was already folded; an untagged line is its own id).
Run tests with `fa test <pytest args>`; it waits for a machine-wide test slot.
