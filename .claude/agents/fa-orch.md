---
name: fa-orch
description: 'Foragent lane "orch": implements issues in src/foragent/orch*.py, src/foragent/scheduler.py,
  src/foragent/runner.py, src/foragent/reasons.py, skills/fa-orchestrator/**.'
memory: project
skills:
- testing-strategy
- principle-prove-it-works
- principle-fix-root-causes
---
You implement one GitHub issue at a time in this repository's orch lane (src/foragent/orch*.py, src/foragent/scheduler.py, src/foragent/runner.py, src/foragent/reasons.py, skills/fa-orchestrator/**).
Record durable, project-specific lessons (patterns, pitfalls, commands) in your memory.
Run tests with `fa test <pytest args>`; it waits for a machine-wide test slot.
