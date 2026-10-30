# Lane dream PRs

Lane memory has two writers (D95). In `.claude/agent-memory/<memory dir>/` (the memory dir is the
lane's `agent`, else the lane name):

- `MEMORY.new.md` is the pending buffer. Lane workers (Claude and Codex get the same instruction)
  and people only append new lessons at its end. Git merges it with `merge=union`.
- `MEMORY.md` is the curated memory. Only the lane's dream edits it. A person's hand edit stays as
  the dream's input; the dream never overwrites it silently.

Every reader shows both files: Codex lane notes, the Claude memory notice, the dream prompt and the
lane view.

Between work, the gate runs a lane's *dream* on branch `foragent/dream-<lane>`, always from the
base. The dream folds the pending lessons into `MEMORY.md` and removes exactly the lines it folded
from the start of `MEMORY.new.md`. Lessons appended later stay. A dream that changed either file
opens a PR titled `Lane <lane> memory: <date>`; its `dream-published` event in `fa orch log` names
the PR. A failed dream publishes nothing, so the base keeps both files. A dream is complete only when
its PR merges: the gate then records `dream-done`. If you close the PR instead, the lane dreams again
from the current base, over the same runs.

- Dream PRs are the exception to SKILL.md's human merge rule (decision in issue #189, 2026-10-27):
  merge an open dream PR yourself, without asking the person, once its CI passes and its diff
  touches only that lane's memory directory (every path in `gh pr diff PR --name-only` starts with
  `.claude/agent-memory/<memory dir>/`). Run
  `fa orch merge PR --approved-by "$(gh api user --jq .login)"`, your own gh login.
- A dream PR whose diff touches anything else is not a memory edit: do not merge it; close it with
  a one-line reason.
- While a lane's dream PR is open, the lane does not dream; merge or close the PR to let it dream
  again. Every dream starts from the base: a dream PR you close is not carried into the next one.
- Before it pushes, the dream checks that the base still holds the two files it read. If the base
  changed, it pushes nothing and the lane dreams again later.
- When the dream opens its PR, the gate records digests of the two files it read on the base and
  wrote on its branch. `fa orch merge` compares the current base and PR head with them, not Git
  history. Any change makes the PR stale, even without a Git conflict and even after a rebase: a
  union merge would bring the folded lessons back. `fa orch merge` refuses a stale dream PR, or one
  with no recorded digests, and names the changed files. Close it with that reason; the lane dreams
  again from the current base. Never merge or rebase a dream PR another way.

## Moving a project to the split

Re-run `fa init` in the project. `fa update` does not migrate. Init changes the agent file of each
lane that sets `agent` (a lane without one has no agent file; its memory dir is the lane name):
it removes `memory: project` and the old "Record durable ... in your memory." line, and adds the
append instruction for `MEMORY.new.md`. It keeps the rest of the agent (body, skills, model) and the
bytes of `MEMORY.md`. Agent files with CRLF line ends or a byte-order mark keep them. In
`.gitattributes`, it moves `merge=union` from `MEMORY.md` to `MEMORY.new.md`. Role agents are not
changed. A second `fa init` changes nothing.

## Lane PRs opened before the split

An open lane PR may still append lessons to `MEMORY.md`. Before you merge it, review the
diff of `.claude/agent-memory/` with the lane agent. Move each appended lesson to the end of
`MEMORY.new.md` on the PR branch and restore `MEMORY.md` to the base. Do not restore a lesson that
a dream already folded out of `MEMORY.new.md`.

## Stale dream PRs #898 and #901

These two dream PRs edited `MEMORY.md` before the split. Handle them in this order:

1. For each PR, record its head, its base and `gh pr diff`.
2. Find lessons that exist only in that PR (not in the current base `MEMORY.md` or
   `MEMORY.new.md`). Append them to the base `MEMORY.new.md`, each with its source
   (`from #898 <head sha>`). Put that change on the base first.
3. Show the person the preservation diff (the lessons you carried and where they came from) and
   ask them to confirm closing both PRs. Do not infer approval.
4. Only after the person confirms: close #898 and #901 without merging, and let the lanes dream
   again from the current base.
