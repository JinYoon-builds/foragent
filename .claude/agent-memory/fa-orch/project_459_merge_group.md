---
name: project-459-merge-group
description: #459 merge groups (r17 single-writer design, 2026-10-30) — where the code lives, what the locked tests require, pitfalls
metadata:
  type: project
---

#459 was rewritten on 2026-10-30 to the r17 single-writer shape on top of #712's FIFO (`run_merge_turn.py`). The old leader-merges-members branch (merge-queue.json, claims file) was dropped; the work restarted from main.

**Why:** the order (D95 state ownership) requires one writer per piece of state: the leader appends only its claim and verdict; each member merges, closes and records only its own PR/issue/run.

**How to apply:**
- Code: `run_merge_group.Membership` (ready fields, `at_turn` consume-or-lead, `base_ok` interval, `complete`, `leave`, `joint_check`); hooks in `run_publish_flow.py` (kept at exactly 300 lines, the split-module limit).
- Group/verdict rows have no `n`; read them with `run_merge_group.rows`. The locked test compares claim `members` with exactly `{n, head_sha}`, so `started_at` sits in a parallel list.
- Record a member's `merged_sha` as the first main commit after its locked base re-read; main's tip can already include an outsider push (the neutral-interval test does this).
- `join(..., fields)` appends a new ready row on a head change only when fields are passed (main path); the reviewed-head path must keep one row (test_short_merge_lock_unit).
- `needs-human` is a merge-decision hold; `test_exact_defaults` needs `merge_group_max: 4`.
- In zsh, `${PIPESTATUS[0]}` is empty; read an exit code with output redirected to a repo-local file and `echo $?`.
- Review 2026-10-30: prove earlier group merges by content (`is_squash_of`: commit tree == merge-tree of the claimed head onto the commit's parent), never by position or a recorded SHA; build the joint tree as sequential squashes with `merge-tree --write-tree` + `commit-tree` (ordinary merges differ when a head reverts an earlier member). A missing baseline is no evidence (capture on the pinned main). `leave()` reads its pass claim from the queue. The leader writes a fail verdict in `finally` around the joint check. Tests here may not run `git commit`/`checkout`; build commits with hash-object/mktree/commit-tree.
