# Lane dream PRs

Between work, the gate runs a lane's *dream*: the lane's implementer tidies its own
`.claude/agent-memory/<memory dir>/MEMORY.md` (the memory dir is the lane's `agent`, else the lane
name) on branch `foragent/dream-<lane>`. A dream that changed the memory opens or updates a PR
titled `Lane <lane> memory: <date>`. Its `dream-done` event in `fa orch log` names the PR.

- A dream PR is ready to merge when its CI passes and its diff touches only that lane's memory
  directory: every path in `gh pr diff PR --name-only` starts with `.claude/agent-memory/<memory dir>/`.
  Merge it through the merge procedure in SKILL.md (`fa orch premerge PR --json`, then
  `fa orch merge PR ...`).
- A dream PR whose diff touches anything else is not a memory edit: do not merge it; close it with
  a one-line reason.
- The dream records its own completion (`fa lanes dreamed <lane>` does the same by hand), so the
  lane can dream again before its PR merges; the next dream adds onto the open PR's branch.
