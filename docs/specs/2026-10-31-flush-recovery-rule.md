# #1043 flush recovery rule (2026-10-31)

Two holds on interrupted flush recovery (HEAD-equality checks; staged leftovers after rollback). Recovery now uses the recorded commit and ancestry, and rollback restores index and worktree.
