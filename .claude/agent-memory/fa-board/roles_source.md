---
name: roles-source
description: roles: source in .foragent.yml (#769) — roles.py is the only writer; init adds defaults; test_init strips roles; global fa rejects new config keys
metadata:
  type: project
---
`foragent/roles.py` owns `roles:` (#769). `replace_block` rewrites only the top-level roles block and checks the result reads back exactly. init appends roles via `_add_config` when the key is missing, and uses `replace_block` when roles exist but defaults are missing (`_config_text`).

- **Why:** every init now writes roles with `created_at`, so exact-text tests in `tests/test_init.py` compare `without_roles(...)`/`config_values(...)` and `added_keys` ends with `"roles"`.
- **How to apply:** once this repo's `.foragent.yml` gains a new config key, the globally installed `fa` (older) fails with "unknown configuration key". Run `uv run fa test ...` from the worktree instead of `fa test`.
- Role commands run real git in the acceptance fixture whose origin is a local path, so `remote_slug()` fails; the orch event is skipped with a warning there.
