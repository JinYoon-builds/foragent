# Claude worker prompt and a live executor check

Evidence (2026-10-23): after `executor: claude` (c219ec7), runner.py sent `/goal <condition>\n\n<whole prompt>` as one message. Claude read all of it as the goal condition and stopped: "Goal condition is limited to 4000 characters (got 30794)", zero turns, cost 0, model "<synthetic>". The runner counted each refusal as an attempt; #163, #178, #184, #210 and #232 spent every attempt with no work. Workers went back to Codex (83e9b2d).

Root cause of it staying hidden: the Claude worker path never ran for real; every test used fake executors.

1. claude-goal-prompt: `/goal` carries only the condition (at most 4000 characters); the prompt reaches Claude separately. A zero-turn / synthetic result with an error text is an executor failure: it spends no attempt, keeps the executor's text in `detail` and is flagged once. Codex argv is unchanged.
2. doctor-live: `fa doctor --live [--executor claude|codex] [--repo-root PATH]` runs the real executor once, headless, on a tiny throwaway task in a temporary clone, through the same runner path real issues use, checks the task result itself, and prints PASS (turns, seconds) or FAIL (the executor's own text) per executor. No GitHub, no pushes; the repo's tree and branches stay unchanged. It needs network and quota, so it runs before merging executor changes and whenever the executor setting changes, not in CI. skills/fa-orchestrator says to run it before switching `executor`.
