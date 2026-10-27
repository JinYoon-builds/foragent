---
name: project-674-skill-space
description: How to free lines in skills/fa-orchestrator/SKILL.md (150-line cap) without breaking the ~16 acceptance files that read it
metadata:
  type: project
---

SKILL.md sat at exactly 150 lines on 2026-10-27, so reflowing wider barely helps (width 116 saved ~10 lines). #674 and #310 (merged the same day) freed space by moving prose no locked test reads into `references/*.md` with a one-line link: watch output fields (`watch.md`), tidy checklist (`tidy.md`), planning-heartbeat question routing (`request-box.md`), notification timing (`notify.md`), order-draft details (`order-card.md`).

**Why:** other acceptance files grep SKILL.md (not references) for phrases; test_pstack_procedures checks that every `references/` link resolves; test_tidy_cycle finds the first heading containing "tidy"; test_orchestrator_by_default splits paragraphs on blank lines.

**How to apply:** before moving text, run `grep -rln fa-orchestrator tests/acceptance` and save the PASSED/FAILED set inside the worktree (`.foragent/`, not /tmp, because files outside the repo must not change); compare after the edit. Related: [[project-156-skill-rewrap]].

Merge with #310: main had created the same `watch.md`/`order-card.md`; keep main's reference files, rebuild SKILL.md from `git show MERGE_HEAD:...` and reapply the issue's edits, then compare reader-test sets with main's SKILL.md vs the merged one. In zsh, split a file list with `${=FILES}` or pytest gets one bogus path.

Pitfall (#674 retry): `grep -l fa-orchestrator tests/acceptance` misses tests that read SKILL.md through another file's helper (`from test_orch_ask_rule import ask_rule_text` in test_question_routing and test_orch_inbox). Build the reader list from both the path grep and imports of text helpers (`ask_rule_text`, `orchestrator_by_default_text`, `settings_skill`, `text` in test_orch_check_loop). The planning-heartbeat routing (120 s, alive → message and no push, absent → push) must stay in SKILL.md; the human-merge detail could move to `references/merge.md` instead.
