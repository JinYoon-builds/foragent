---
name: project-674-skill-space
description: How to free lines in skills/fa-orchestrator/SKILL.md (150-line cap) without breaking the ~16 acceptance files that read it
metadata:
  type: project
---

SKILL.md sat at exactly 150 lines on 2026-10-27, so reflowing wider barely helps (width 116 saved ~10 lines). #674 freed space by moving prose no locked test reads into `references/*.md` with a one-line link: watch output fields (`watch.md`), tidy checklist (`tidy.md`), planning-heartbeat question routing (`request-box.md`), notification timing (`notify.md`), order-draft details (`order-card.md`).

**Why:** other acceptance files grep SKILL.md (not references) for phrases; test_pstack_procedures checks that every `references/` link resolves; test_tidy_cycle finds the first heading containing "tidy"; test_orchestrator_by_default splits paragraphs on blank lines.

**How to apply:** before moving text, run `grep -rln fa-orchestrator tests/acceptance` and save the PASSED/FAILED set inside the worktree (`.foragent/`, not /tmp, because files outside the repo must not change); compare after the edit. Related: [[project-156-skill-rewrap]].
