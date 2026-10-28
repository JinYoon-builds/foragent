# draft, not posted

**Title:** Show HN: Foragent – Claude Code picks up the next issue by itself

**URL:** https://github.com/JinYoon-builds/foragent

**Text:**

Stock Claude Code is already good at finishing one task. What I kept doing by hand was the part between tasks: "are you done? run the checks, review it, merge it, now take the next one."

Foragent automates that loop:

    task ends → done-when commands → adversarial review → merge → next issue

You describe what to build in `/fa-order`. It interviews you, splits the work into GitHub issues with executable acceptance tests, and asks for one approval. After that, `fa gate` (a background program with no LLM of its own) runs each issue: it starts a Claude Code session, runs the done-when commands, sends the diff to an adversarial review, opens the PR, merges it when it passes, and moves on.

Work is split into lanes. A lane is an agent that owns one area of the code. Each lane has its own paths, its own memory of patterns and pitfalls in that area, and its own git worktree, so lanes run in parallel without editing the same files.

It runs on stock Claude Code: lanes are `.claude/agents` files, skills are `SKILL.md`, memory is project memory. Codex CLI is optional, for implementation or cross-review.

Foragent built itself: 208 merged PRs in 10 days, counted from `git log` on 2026-10-29.

Known limits, stated up front:

- Claude Code is required. Codex-only setups are not supported yet.
- The background gate is macOS only (launchd). No Linux service yet.
- Native Windows is not supported. WSL will follow the Linux gate.
- Only Orca can switch back to the terminal Foragent opened before. tmux, WezTerm, kitty, iTerm2 and Terminal get a new split instead.
- Update checks always use the main repository, even from a fork.

Each limit in the README names the file where a fix would start. Contributions are welcome.

Install:

    uv tool install git+https://github.com/JinYoon-builds/foragent
    fa init

Then type `/fa` in Claude Code. I'd love feedback, especially on how the lanes split work in a codebase that isn't Foragent itself.
