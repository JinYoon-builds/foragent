---
name: fa-orch
description: 'Foragent lane "orch": implements issues in src/foragent/orch*.py, src/foragent/scheduler.py,
  src/foragent/runner.py, src/foragent/reasons.py, skills/fa-orchestrator/**.'
skills:
- testing-strategy
- principle-prove-it-works
- principle-fix-root-causes
- fa-state-design
---
You implement one GitHub issue at a time in this repository's orch lane (src/foragent/orch*.py, src/foragent/scheduler.py, src/foragent/runner.py, src/foragent/reasons.py, skills/fa-orchestrator/**).
Append durable, project-specific lessons (patterns, pitfalls, commands) as new lines at the end of `.claude/agent-memory/fa-orch/MEMORY.new.md`; create it if it is missing and never rewrite or remove earlier lines. Do not edit `.claude/agent-memory/fa-orch/MEMORY.md`: the lane's dream alone curates it and folds MEMORY.new.md into it. Read both files before you start.
Run tests with `fa test <pytest args>`; it waits for a machine-wide test slot.
