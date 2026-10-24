# Notice early quota resets for free

Evidence (2026-10-24): Codex's weekly quota reset early (it had announced 10-30); Foragent waited for the announced time and the quota sat idle. Today Foragent only reads limit errors and pauses the lane for limit_wait_minutes (executors.py:180,229; runner.py:1006).

Free sources (researched; no model turn): Codex app-server JSON-RPC `account/rateLimits/read` -> {rateLimits:{primary,secondary}} windows {usedPercent, windowDurationMins, resetsAt}. Claude Code statusline input JSON -> `rate_limits.{five_hour,seven_day}.{used_percentage, resets_at}`, filled after API responses. Jin chose (2026-10-24): Codex via app-server; Claude via a statusline wrapper (the undocumented /api/oauth/usage is not used).

1. usage-recheck: while an executor is marked exhausted (#105), the gate re-reads its usage from the free source every `usage_recheck_minutes` (default 10); when used drops below the exhausted threshold the pause clears early with a `quota-recovered` event naming executor and source. The announced reset time is only the upper bound; a missing, failing or stale (older than 2 x interval) source keeps today's behaviour (announced reset, else limit_wait_minutes). Higher usage never shortens a pause. The re-read never starts a model turn.
2. statusline-wrapper: `fa statusline` reads statusline JSON on stdin, writes `rate_limits` with the time seen to `claude-rate-limits.json` under the Foragent data root, then runs the user's previous statusline command with the same stdin and passes its output and exit code through unchanged. `fa init` asks once before installing it, saves the previous command, never double-wraps, and remembers a refusal.
Installing the wrapper into Jin's own ~/.claude settings is Jin's step to approve when this lands.
