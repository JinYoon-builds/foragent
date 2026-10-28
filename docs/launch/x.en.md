# draft, not posted

**Post 1/3** (GIF goes here)

Claude Code is already good at finishing one task.

Foragent automates the part between tasks:

task ends → done-when checks → adversarial review → merge → next issue

You decide what to build and approve it once.

https://github.com/JinYoon-builds/foragent

**Post 2/3**

Work is split into lanes. Each lane is a plain .claude/agents file that owns one area of your code, keeps its own memory, and runs in its own git worktree.

Foragent built itself: 208 merged PRs in 10 days.

**Post 3/3**

Known limits: needs Claude Code (Codex optional), the background gate is macOS only, no native Windows yet.

Each limit in the README names the file where a fix would start. PRs welcome.
