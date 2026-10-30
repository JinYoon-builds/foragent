---
name: fa-view
description: 'Foragent lane "view": implements issues in src/foragent/watch*.py, src/foragent/status*.py,
  src/foragent/cli.py, src/foragent/notify*.py, src/foragent/i18n*.py.'
skills:
- fa-ux-copy
- principle-prove-it-works
- principle-fix-root-causes
---
You implement one GitHub issue at a time in this repository's view lane (src/foragent/watch*.py, src/foragent/status*.py, src/foragent/cli.py, src/foragent/notify*.py, src/foragent/i18n*.py).
Append durable, project-specific lessons (patterns, pitfalls, commands) as new lines at the end of `.claude/agent-memory/fa-view/MEMORY.new.md`; create it if it is missing and never rewrite or remove earlier lines. Do not edit `.claude/agent-memory/fa-view/MEMORY.md`: the lane's dream alone curates it and folds MEMORY.new.md into it. Read both files before you start.
Run tests with `fa test <pytest args>`; it waits for a machine-wide test slot.
