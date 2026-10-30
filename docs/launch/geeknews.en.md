# draft, not posted

**Title:** Foragent – Claude Code picks up the next issue when a task ends

**Link:** https://github.com/JinYoon-builds/foragent

(GeekNews is a Korean site. This is the English review copy of geeknews.ko.md; post the Korean file.)

- Stock Claude Code is already good at finishing one task. Foragent automates the part **between tasks**.
  - When a task ends, it runs the done-when commands, sends the change to an adversarial review, merges it when it passes, and picks up the next issue.
  - You decide what to build and approve it once. No more "done? → review it → next one" by hand.
- **Order `/fa-order`**: it interviews you and splits the work into GitHub issues with executable acceptance tests.
- **Run `fa gate`**: a background program with no LLM of its own. For each approved issue it starts a coding session (Codex CLI when it is logged in, else Claude Code): implement → check → review by the other tool → PR → merge. It keeps going while you're away.
- **Lanes**: a lane is an agent that owns one area of your code.
  - Each lane has its own paths, so two lanes don't edit the same files.
  - Each lane keeps its own memory of patterns, pitfalls and commands, and uses it on the next issue.
  - Lanes work at the same time in their own git worktrees. The gate merges the results one by one.
- **Runs on stock Claude Code**: lanes are `.claude/agents`, skills are `SKILL.md`, memory is project memory. Codex CLI is optional: when it is logged in, it implements and Claude Code reviews.
- Foragent built itself: 208 merged PRs in 10 days (counted from `git log` on 2026-10-29).
- **Known limits** (the README names the file where each fix would start; contributions welcome)
  - Claude Code is required. Codex-only setups aren't supported yet.
  - The background gate is macOS only. No Linux service yet.
  - Native Windows isn't supported yet.
  - Only Orca can switch back to the terminal Foragent opened before.
  - Update checks always use the main repository, even from a fork.
- Install: `uv tool install git+https://github.com/JinYoon-builds/foragent`, then `fa init` in your project folder and `/fa` in Claude Code.
