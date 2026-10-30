# #466 committed-diff guard (2026-10-30)

Jin chose option A (architect r41): after 8 holds, each finding another post-guard worktree mutation, the guard checks only the committed diff of each measured head sha. Worktree-only tricks are left to review and GitHub CI; recovery is a revert.
