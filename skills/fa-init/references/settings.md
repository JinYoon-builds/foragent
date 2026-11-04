# First-use settings questions

After the ordered setup checklist passes, `/fa-init` asks only two configuration
questions: merge (review) mode, then Claude quota mode. Checklist fixes (including
installing tools and creating or linking a repository) require the person's
agreement in the `/fa` checklist walkthrough first. Login and permission commands
are shown for the person to run.
Setup also asks about the optional project FA permission rule when
it is undecided, as described in [the setup flow](../SKILL.md). It says nothing
about credits or spending; those are asked just in time elsewhere.

Order:

1. Ask merge mode: fast (recommended) or careful.
2. After setup agreement, run `fa init --review <mode>` in the background and wait
   for its result. Leave `--executor` off so the config default applies, and never
   pass `--claude-quota`: the quota answer belongs to the person's own `me` scope, not to git.
3. Only if Claude is available, ask quota mode: keep (recommended) or burn. Claude is available when
   `claude auth status` reports logged in, or `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` is set.
   Otherwise skip the question.
4. Save the quota answer with `fa settings set claude_quota.mode <mode>`.

Re-runs: read each key in its own scope file directly. Skip the merge question when `.foragent.yml`
already has a `review` key; skip the quota question when the user config
(`~/.config/foragent/config.yml`) already has `claude_quota.mode`. A first run always asks both
(unless Claude is unavailable).

Later changes use the same command: `fa settings set review <mode>`,
`fa settings set claude_quota.mode <mode>`; `fa settings list` shows every key with its scope.
