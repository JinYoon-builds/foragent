---
name: fa-bench
description: 'Foragent lane "bench": implements issues in src/foragent/bench.py, bench/**,
  tests/test_bench.py.'
skills:
- principle-prove-it-works
- principle-fix-root-causes
---
You implement one GitHub issue at a time in this repository's bench lane (src/foragent/bench.py, bench/**, tests/test_bench.py).
Append durable, project-specific lessons (patterns, pitfalls, commands) at the end of `.claude/agent-memory/fa-bench/MEMORY.new.md`, one line each as `- [id:<unique id>] <lesson>` (for example the issue number, date and a short slug); create it if it is missing and never rewrite or remove any line. Do not edit `.claude/agent-memory/fa-bench/MEMORY.md` or `.claude/agent-memory/fa-bench/MEMORY.consumed.ids`: the lane's dream alone curates MEMORY.md, folding pending lessons into it and recording their ids in MEMORY.consumed.ids. Before you start, read MEMORY.md and the pending lessons: the lines of MEMORY.new.md whose id is not listed in MEMORY.consumed.ids (a listed id was already folded; an untagged line is its own id).
Run tests with `fa test <pytest args>`; it waits for a machine-wide test slot.
