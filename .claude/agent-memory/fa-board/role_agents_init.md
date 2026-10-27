---
name: role-agents-init
description: How fa init ships role agents/skills (#717) — package data via setuptools data-files, recovery of uncommitted setup files, test_init path lists
metadata:
  type: project
---
fa init creates the fa-orchestrator and fa-architect agents, their memory and their skill copies through `role_agents.missing_files(root)`. It merges these files into init's `contents`, so they share the setup files' created, dry-run and commit flow (#717).

- The skills sit at the repository root (`skills/<role>`), outside the package. Only pyproject.toml was in scope, so the wheel ships them with `[tool.setuptools.data-files]` under `share/foragent/skills`. `skill_source` checks the source checkout first, then `sysconfig.get_path("data")`. To check a change, run `uv build --wheel` and list the wheel with `unzip -l`.
- **Why:** if a commit hook fails, the files stay staged. On the next run they already exist, so they drop out of `created` and never get committed. `role_agents.setup_paths` returns only the files whose content still matches what init writes, and init passes them to `git.uncommitted_paths`. This way init recovers its own files and never commits an edit the user made.
- **How to apply:** whenever init creates a new kind of file, also update the exact path lists in `tests/test_init.py`. Use `ALL_PATHS = [*PATHS, *ROLE_PATHS]`. Files that init writes later come after the setup files.
