---
name: project-716-role-agents
description: #716 orchestrator/architect role agents; architect prompt vs skill split and the pstack refactor.md constraint
metadata:
  type: project
---
- `orch_architect.build_prompt(why, root, executor)`: Claude gets a pointer (agent loads the skill via `session_options` → `agent="fa-architect"` when `.claude/agents/fa-architect.md` exists); Codex gets `.claude/skills/fa-architect/SKILL.md` text, else `skills/fa-architect/SKILL.md`, else pointer. The prompt must keep test_orch_architect regex phrases but none of test_role_agents' PROCEDURE_PHRASES (those live only in the skill).
- `orch_spawn._command` inserts `--agent fa-orchestrator` before `/fa-orchestrator` (the argv token liveness checks look for).
- **Why:** locked `test_pstack_procedures[refactor.md]` requires refactor.md to keep the pstack credit/MIT and the reproduce/evidence skeleton, and bans the phrase "architect skill" (regex `architect\s+skills?`). A text summary in refactor.md was rejected by review as duplicated procedure; a symlink was then rejected because SkillManager.check_install and copy_lane_skills refuse symlinks inside skills/. Never put symlinks under skills/; the pointer-vs-pstack conflict went to QUESTION.md.
- **How to apply:** when editing skills/fa-orchestrator/references/*.md, run tests/acceptance/test_pstack_procedures.py too.
