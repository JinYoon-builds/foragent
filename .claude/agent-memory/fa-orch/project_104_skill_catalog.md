---
name: project-104-skill-catalog
description: #104 per-issue skill catalog in the implementer prompt and Skills used report in run state and PR body
metadata:
  type: project
---
- `run_skill_catalog.py`: `available_skills()` lists skills with a confirmed non-style role in `skill_roles` (SkillManager `available_only=True`, no clone), minus lane fixed skills. `catalog_section()` is always appended in `_implement()` for both executors, even when empty, so every run is asked for `Skills used:` (review point: an ordinary run must not skip the report).
- Wording lives in `messages/skills_used.py` (en only; prompt and PR body are English); the report-parse prefix is derived from the `skills_used_line` template.
- `_record_skills_used()` unions the last `Skills used:` line of each implementer result into Run `skills_used`/`skills_reported`; the PR body always has the line (`not reported` when missing).
- Never weaken review validation for a fixture: the first #104 attempt let `parse_code_review()` accept plain `VERDICT:` text; the orchestrator reverted it and fixed the locked test to feed a real `/code-review` result event instead.
- CI lesson: never append to the implementer prompt after `build_prompt()` in `_implement()`. `tests/test_run_prompts.py` compares the call prompt to `build_prompt(...)` exactly, and `tests/test_order.py::test_preread_setup_matches_the_implementation_setup` patches `build_prompt` to `"PROMPT"` and expects `notes + "PROMPT"`. New prompt sections go in as `build_prompt` kwargs (here `skill_catalog=`), added before the hint and `/goal` wrapping.
