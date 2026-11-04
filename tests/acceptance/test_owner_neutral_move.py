"""Acceptance test for owner-neutral-move. Locked by Foragent; do not edit.

With the merge owner on, main often moves only by bookkeeping commits: role
memory under .claude/agent-memory/, docs and order specs under docs/, and the
locked acceptance files of other open, approved issues. Such a move cannot
change a queued PR's result. The run's own base-move rule already keeps the
verified head and its passed CI for it (#376, #514, #1688). The merge owner
must honour the same rule: the PR is not refreshed onto the moved base, no new
CI round starts, and the green PR merges with the head its CI measured. A move
that touches source still refreshes the PR, once, as before.

Real runner, gate tick, merge owner, Git and state; only GitHub, models and the
clock are fake (the merge-owner prototype world).
"""

from acceptance.test_merge_path_real_settings import PYTHON, world  # noqa: F401
from conftest import git_command
from test_merge_owner_proto import proto  # noqa: F401
import test_merge_owner_proto as support
from foragent.issue import body_hash

ORDER = 601
ORDER_TEST = f"tests/acceptance/test_order_{ORDER}.py"


def remote_head(case, n):
    return git_command(case.history.origin, "rev-parse", f"refs/heads/foragent/issue-{n}")


def main_file(case, path):
    return git_command(case.history.origin, "show", f"main:{path}")


def approve_order(case):
    """Another open issue, approved, that owns a locked file that just landed on main."""
    body = ("<!-- foragent:v1 -->\n## Goal\nDeliver the ordered feature.\n"
            "## Out of scope\nMembers.\n## Files\n"
            f"- src/feature_{ORDER}.py\n- {ORDER_TEST}\n## Done when\n```sh\n"
            f"{PYTHON} -m pytest -q -p no:cacheprovider {ORDER_TEST}\n```\n")
    case.tracker.add_issue(ORDER, "Ordered feature", body)
    case.tracker.record_label(ORDER, "approved", "human")
    git_command(case.root, "fetch", "origin")
    case.store.save_approval(ORDER, body=body, body_sha=body_hash(body),
                             acceptance_blobs=case.git.blob_shas("origin/main", [ORDER_TEST]),
                             approved_by="human", approved_at=case.clock[0].isoformat())


def bookkeeping_commits(case):
    """The commit shapes that moved main 24 of 29 times on 2026-11-04, one commit each."""
    commits = [
        {".claude/agent-memory/fa-orch/MEMORY.md": "- lesson one\n"},
        {"docs/specs/2026-11-04-order-a.md": "# Order A\n"},
        {".claude/agent-memory/fa-docs/MEMORY.md": "- lesson two\n"},
        {"docs/specs/2026-11-04-order-b.md": "# Order B\n",
         ORDER_TEST: ("from pathlib import Path\n\ndef test_order():\n"
                      f"    assert Path('src/feature_{ORDER}.py').is_file()\n")},
        {".claude/agent-memory/fa-orch/MEMORY.md": "- lesson one\n- lesson three\n"},
    ]
    for files in commits:
        case.history.update(files, message="Foragent bookkeeping")
        if ORDER_TEST in files:
            approve_order(case)
    return [path for files in commits for path in files]


def test_bookkeeping_moves_merge_green_pr_without_refresh_or_new_ci(proto):
    case = proto
    support.queue_workers(case, [5])
    pr = support.pr_for(case, 5).number
    queued = remote_head(case, 5)
    rounds = len(case.ci.by_head)

    paths = bookkeeping_commits(case)  # Five main moves land while the green PR waits.
    support.drain(case, count=1)

    assert case.tracker.merged_prs == [pr], "The green PR merges once"
    assert remote_head(case, 5) == queued, "A result-neutral move must not refresh (push) the PR"
    assert len(case.ci.by_head) == rounds, "A result-neutral move must not start a new CI round"
    assert case.merges[-1]["head"] == queued, "The merge is of the head the passed CI measured"
    assert case.tracker.get_issue(5).state == "closed"
    assert main_file(case, "src/member_5.py") == "VALUE = 5"
    for path in paths:
        assert main_file(case, path), f"The merge keeps the bookkeeping file {path} on main"


def test_source_move_after_bookkeeping_refreshes_exactly_once(proto):
    case = proto
    support.queue_workers(case, [5])
    pr = support.pr_for(case, 5).number
    queued = remote_head(case, 5)
    rounds = len(case.ci.by_head)
    case.tracker.set_pr_checks(pr, "pending")  # The PR waits while main moves.

    for files in ({".claude/agent-memory/fa-orch/MEMORY.md": "- lesson\n"},
                  {"docs/specs/2026-11-04-order-c.md": "# Order C\n"}):
        case.history.update(files, message="Foragent bookkeeping")
        support.gate(case)
    assert remote_head(case, 5) == queued, "Bookkeeping moves must not refresh the waiting PR"
    assert len(case.ci.by_head) == rounds

    # One commit that also touches source is not result-neutral.
    case.history.update({".claude/agent-memory/fa-docs/MEMORY.md": "- note\n",
                         "src/neighbor.py": "VALUE = 'moved during the queue'\n"},
                        message="Source and memory")
    support.gate(case)
    refreshed = remote_head(case, 5)
    assert refreshed != queued, "A source move refreshes the PR onto the moved base"
    assert not case.tracker.merged_prs, "The refreshed head waits for its own CI round"

    case.tracker.set_pr_checks(pr, "pass")
    support.drain(case, count=1)

    assert len(case.ci.by_head) == rounds + 1, "Exactly one refresh and one new CI round in total"
    assert case.tracker.merged_prs == [pr]
    assert case.merges[-1]["head"] == refreshed, "The merge is of the refreshed, re-verified head"
    assert main_file(case, "src/neighbor.py") == "VALUE = 'moved during the queue'"
    assert main_file(case, "src/member_5.py") == "VALUE = 5"
    assert main_file(case, "docs/specs/2026-11-04-order-c.md") == "# Order C"
