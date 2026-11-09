"""Unlocked regression guard for one-commit-per-pass (not in Done when).

An owner pass with no queued non-PR main-write request makes no commit and no
push on main. This already holds on main f27962b; it guards the one-commit
change from adding an empty commit or push per pass. Fixtures come from the
locked tests/acceptance/test_one_commit_per_pass.py.

Break-it: an empty commit or a push per pass moves the tip or the push count.
"""

from test_one_commit_per_pass import case, offline_publication, owner, owner_pass, pushes, tip  # noqa: F401  (fixtures)


def test_pass_with_no_request_makes_no_commit_and_no_push(owner):
    o = owner
    before, pushed = tip(o), pushes(o)
    owner_pass(o)
    owner_pass(o)
    assert tip(o) == before, "an owner pass with no queued request must not move main"
    assert pushes(o) == pushed, "an owner pass with no queued request must not push"
