# pstack principles (vendored)

Foragent ships the 24 portable principle skills of **pstack by Lauren Tan**
(MIT License, Copyright (c) 2026 Lauren Tan) at a pinned upstream revision.

- Upstream: https://github.com/cursor/plugins/tree/9b93e81fb2921b9263d9463c15bafeabfe3903aa/pstack
- Revision: `9b93e81fb2921b9263d9463c15bafeabfe3903aa`
- License: [LICENSE](LICENSE) (the full upstream MIT text); each skill folder also carries it.
- Source paths: `pstack/skills/<name>/SKILL.md`, listed one per skill in [library.yml](library.yml).

The skills live in `skills/principle-*/`. `library.yml` is the only list of
which folders belong to this library. `fa skills catalog` shows them as
`pstack:<name>` with license MIT, and `fa skills install pstack:<name> --repo-root .`
copies only the chosen skill into `.claude/skills/`. Nothing installs them
automatically. `fa skills sync` never pulls this library: it is pinned, and
pulling would move the checkout that contains it.

## What was taken

Every `pstack/skills/principle-*` folder at the revision: 24 skills, each a
single `SKILL.md`. Nothing else from pstack is vendored. The procedures
(poteto-mode, arena, swarm, interrogate and the rest), agents, automations,
setup and Cursor plugin files are not part of the principle set.

## Edits

- `principle-prove-it-works` and `principle-fix-root-causes` were imported
  earlier (#725, source commit 2171859). They are kept byte for byte; their own
  `Source:`/`Changed:` lines describe their edits.
- The other 22 are the upstream text with two kinds of change, each stated in
  the skill's `Changed:` line:
  - `disable-model-invocation: true` is removed, so an agent can load the
    skill after it installs it. This is the same change the two earlier imports made.
  - A reference to a skill Foragent does not ship is dropped:
    `benchmark-checklist` in `principle-explain-the-number` and
    `typescript-best-practices` in `principle-type-system-discipline`.
- Links between principles (`../principle-<name>/SKILL.md`) are kept. They
  resolve when the linked principle is installed too.

## Portability check

Read as an agent at the pinned revision: no principle text depends on Cursor
controls, model routing, rules files or plugin setup. "Subagents" is used in
the general sense that Claude Code and Codex also support.
