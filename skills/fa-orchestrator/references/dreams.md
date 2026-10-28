# Lane dream PRs

Between work, the gate runs a lane's *dream*: the lane's implementer tidies its own
`.claude/agent-memory/<memory dir>/MEMORY.md` (the memory dir is the lane's `agent`, else the lane
name) on branch `foragent/dream-<lane>`. A dream that changed the memory opens or updates a PR
titled `Lane <lane> memory: <date>`. Its `dream-done` event in `fa orch log` names the PR.

- Dream PRs are the exception to SKILL.md's human merge rule (decision in issue #189, 2026-10-27):
  merge an open dream PR yourself, without asking the person, once its CI passes and its diff
  touches only that lane's memory directory (every path in `gh pr diff PR --name-only` starts with
  `.claude/agent-memory/<memory dir>/`). Run
  `fa orch merge PR --approved-by "$(gh api user --jq .login)"`, your own gh login.
- A dream PR whose diff touches anything else is not a memory edit: do not merge it; close it with
  a one-line reason.
- While a lane's dream PR is open, the lane does not dream; merge or close the PR to let it dream
  again. Every dream starts from the base: a dream PR you close is not carried into the next one.
