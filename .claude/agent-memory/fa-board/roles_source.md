---
name: roles-source
description: roles: source in .foragent.yml (#769) — roles.py is the only writer; init adds defaults; test_init strips roles; global fa rejects new config keys
metadata:
  type: project
---
`foragent/roles.py` owns `roles:` (#769). `edit_roles` edits line by line: only added/removed entries and changed fields of a changed entry are rewritten (review #769 blocked a whole-block rewrite for losing comments); it checks the result reads back exactly and no comment outside rewritten lines is lost. init appends roles via `_add_config` when the key is missing, and uses `edit_roles` when roles exist but defaults are missing (`_config_text`).

- **Why:** every init now writes roles with `created_at`, so exact-text tests in `tests/test_init.py` compare `without_roles(...)`/`config_values(...)` and `added_keys` ends with `"roles"`.
- **How to apply:** once this repo's `.foragent.yml` gains a new config key, the globally installed `fa` (older) fails with "unknown configuration key". Run `uv run fa test ...` from the worktree instead of `fa test`.
- Role commands run real git in the acceptance fixture whose origin is a local path, so `remote_slug()` fails; the event store then uses `"local" + root path` as key (skipping the event was blocked in review). Default role agents created by init take skills from the roles entry (`role_agents.missing_files(root, skills)`), never the template. Skills resolve also from an installed `.claude/skills/<name>/` copy.
- Role verbs commit only paths whose bytes changed (`_Edit.snapshot`/`publish`) and always append the orch event; `git status` (`uncommitted_paths`) inside `fa role` trips the locked init fixture's no-sleep/kill guard, so do not call it there. An existing agent file keeps its body even when empty; only a missing file gets the generated body (#769 review).
- Every role verb derives all three projections: entry, agent file and memory (`_ensure_memory`, created only when missing). `role_drift` reports a missing agent or MEMORY.md so `fa role set <name>` is the repair path (#769 review: set skipped memory).
