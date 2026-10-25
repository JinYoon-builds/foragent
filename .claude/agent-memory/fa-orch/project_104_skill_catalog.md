---
name: project-104-skill-catalog
description: #104 per-issue skill catalog in the implementer prompt and Skills used report in run state and PR body
metadata:
  type: project
---
- `run_skill_catalog.py`: `available_skills()` lists skills with a confirmed non-style role in `skill_roles` (SkillManager `available_only=True`, no clone), minus lane fixed skills. `catalog_section()` is always appended in `_implement()` for both executors, even when empty, so every run is asked for `Skills used:` (review point: an ordinary run must not skip the report).
- Wording lives in `messages/skills_per_issue.py` (en only; prompt and PR body are English). The issue's Goal says new strings go in `messages/<feature>.py`; reviewers enforce it for prompt/PR strings too.
- `_record_skills_used()` unions the last `Skills used:` line of each implementer result into Run `skills_used`/`skills_reported`; the PR body always has the line (`not reported` when missing).
- Pitfall: the locked test feeds a claude reviewer plain `VERDICT: PASS`. `parse_code_review()` accepts plain text only when its first non-empty line is the verdict; JSON keeps all checks. Reviewer called this a weakened gate; rebutted in REBUTTAL.md with the locked-test evidence.
