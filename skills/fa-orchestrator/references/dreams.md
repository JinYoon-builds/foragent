# Lane dream PRs

Lane memory has two writers (D95). In `.claude/agent-memory/<memory dir>/` (the memory dir is the
lane's `agent`, else the lane name):

- `MEMORY.new.md` is the pending buffer. Lane workers (Claude and Codex get the same instruction)
  and people only append lessons at its end, one per line as `- [id:<id>] <lesson>`; an untagged
  line is its own id. Nobody rewrites or truncates it. Git merges it with `merge=union`.
- `MEMORY.md` is the curated memory. Only the lane's dream edits it. A person's hand edit stays as
  the dream's input; the dream never overwrites it silently.
- `MEMORY.consumed.ids` lists the ids the dream folded or dropped, one per line. Only the dream
  appends to it.

The pending lessons are derived: the lines of `MEMORY.new.md` whose id is not in
`MEMORY.consumed.ids`, each tagged id once. A union merge, a rebase or a retried append can repeat
or reorder lines, but it cannot bring back a consumed id, so lane PRs merge as usual.

Every reader shows the curated memory and the pending lessons: Codex lane notes, the Claude memory
notice, the dream prompt and the lane view. Every lane executor gets the same append-only
instruction: from its agent file, or from the gate when the lane has no agent file or its agent is
not migrated yet.

Between work, the gate runs a lane's *dream* on branch `foragent/dream-<lane>`, always from the
base. The dream folds the pending lessons into `MEMORY.md` and appends their ids to
`MEMORY.consumed.ids`; `MEMORY.new.md` stays byte for byte (the gate puts it back if the dream
changed it, and ends a dream that rewrote earlier ids). A dream that changed its files opens a PR
titled `Lane <lane> memory: <date>`; its `dream-published` event in `fa orch log` names the PR. A
failed dream publishes nothing, so the base keeps every file. A dream is complete only when its PR
merges: the gate then records `dream-done`. If you close the PR instead, the lane dreams again from
the current base, over the same runs.

- Dream PRs are the exception to SKILL.md's human merge rule (decision in issue #189, 2026-10-27):
  merge an open dream PR yourself, without asking the person, once its CI passes and its diff
  touches only that lane's memory directory (every path in `gh pr diff PR --name-only` starts with
  `.claude/agent-memory/<memory dir>/`). Run
  `fa orch merge PR --approved-by "$(gh api user --jq .login)"`, your own gh login.
- A dream PR whose diff touches anything else is not a memory edit: do not merge it; close it with
  a one-line reason.
- While a lane's dream PR is open, the lane does not dream; merge or close the PR to let it dream
  again. Every dream starts from the base: a dream PR you close is not carried into the next one.
- Before it pushes, the dream checks that the base still holds the `MEMORY.md` and
  `MEMORY.consumed.ids` it read. If either changed, it pushes nothing and the lane dreams again
  later. Lessons appended to `MEMORY.new.md` meanwhile do not matter.
- When the dream opens its PR, the gate records digests of those two files as read on the base and
  as written on its branch. `fa orch merge` compares the current base and PR head with them. A
  change (a hand edit of `MEMORY.md` on the base, or a PR head that is no longer the dream's work)
  makes the PR stale: `fa orch merge` refuses it, or one with no recorded digests, and names the
  changed files. Close it with that reason; the lane dreams again from the current base.

## Moving a project to the split

Re-run `fa init` in the project. `fa update` does not migrate. Init changes the agent file of each
lane that sets `agent` (a lane without one has no agent file; its memory dir is the lane name):
it removes `memory: project` (read as YAML, in any spelling) and the old "Record durable ... in
your memory." line, and adds the append instruction for `MEMORY.new.md`. It keeps the rest of the
agent (body, skills, model) and the bytes of `MEMORY.md`. Agent files with CRLF line ends or a
byte-order mark keep them. In `.gitattributes` (read with Git's pattern rules), it removes
`merge=union` from lines about lane `MEMORY.md` files and keeps their other attributes; a broader
union line stays and is followed by `.claude/agent-memory/**/MEMORY.md !merge`. It adds the
`MEMORY.new.md` union line unless that file already merges by union. Role agents are not changed.
A second `fa init` changes nothing.

## Lane PRs opened before the split

An open lane PR may still append lessons to `MEMORY.md`. Before you merge it, review the
diff of `.claude/agent-memory/` with the lane agent. Move each appended lesson to the end of
`MEMORY.new.md` on the PR branch as `- [id:pr<PR>-<n>] <lesson> (from #<PR> <head sha>)`, and
restore `MEMORY.md` to the base. The PR's merge keeps the moved lines and their source in Git
history.

## Stale dream PRs #898 and #901

These two dream PRs edited `MEMORY.md` before the split. Closing them is the only step that cannot
be undone, so every piece of evidence is on the base first, in the lane's `MEMORY.new.md`. Handle
them in this order:

1. For each PR, append one evidence block to the end of that lane's base `MEMORY.new.md`:

   ````markdown
   ## Carried from dream PR #898 (head <head sha>, base <base sha>)
   - [id:pr898-<n>] <each lesson that exists only in this PR> (from #898 <head sha>)

   ```diff
   <the full output of `gh pr diff 898`>
   ```
   ````

   Read the head and base with `gh pr view 898 --json headRefOid,baseRefOid`. A lesson exists
   only in the PR when it is in neither the current base `MEMORY.md` nor its pending lessons. Write
   `- (none)` when no lesson is unique; the head, base and diff are still recorded.
2. Put that change on the base (a normal lane PR that touches only `MEMORY.new.md`) and wait until
   it merges. Check with `git show origin/<base>:<path to MEMORY.new.md>` that both blocks are
   there.
3. Show the person the preservation diff (the merged change from step 2) and ask them to confirm
   closing both PRs. Do not infer approval.
4. Only after the person confirms: close #898 and #901 without merging, with a comment that names
   the commit from step 2, and let the lanes dream again from the current base. The dream folds the
   carried lessons and records their ids; the evidence block stays in `MEMORY.new.md` and in Git
   history.
