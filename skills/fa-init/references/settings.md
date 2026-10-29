# First-use settings questions

`/fa-init` asks two questions and nothing else about settings: merge (review) mode, then Claude
quota mode. It says nothing about credits or spending; those are asked just in time elsewhere.

Order:

1. Ask merge mode: batch (recommended), fast or careful.
2. Run `fa init --review <mode>`. Leave `--executor` off so the config default applies, and never
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
