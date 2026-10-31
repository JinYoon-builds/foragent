# #1043 never undo a batch (2026-10-31)

Sixth hold, all on undo paths. A batch commit is never undone; failed pushes are retried; one pending batch at most; flush only on the base branch.
