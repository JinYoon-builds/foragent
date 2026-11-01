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
Append durable, project-specific lessons (patterns, pitfalls, commands) at the end of `.claude/agent-memory/fa-orch/MEMORY.new.md`, one line each as `- [id:<unique id>] <lesson>` (for example the issue number, date and a short slug); create it if it is missing and never rewrite or remove any line. Do not edit `.claude/agent-memory/fa-orch/MEMORY.md` or `.claude/agent-memory/fa-orch/MEMORY.consumed.ids`: the lane's dream alone curates MEMORY.md, folding pending lessons into it and recording their ids in MEMORY.consumed.ids. Before you start, read MEMORY.md and the pending lessons: the lines of MEMORY.new.md whose id is not listed in MEMORY.consumed.ids (a listed id was already folded; an untagged line is its own id).
Run tests with `fa test <pytest args>`; it waits for a machine-wide test slot.

GitHub writes (issues, pull requests, comments, labels, board items and fields) go through FA commands such as `fa order`, `fa inspect` and `fa orch`. Read-only `gh` inspection stays available. If no FA command fits a write you need, do not hand the write to the person: propose an issue that asks for the missing FA command.
