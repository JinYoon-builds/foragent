# #459 harness fix (2026-10-26)

In test_merge_group.py, a member whose PR the leader already merged still acquires its own merge.lock request (as the issue requires) and finds its PR closed, so the shared hold record marks it not green. assert_group_short_holds now accepts such a hold when it is empty.
