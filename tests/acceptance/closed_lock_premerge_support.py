"""Shared world for alarm-ignores-premerge-base acceptance tests (2026-11-09).

Builds on the locked closed-lock-red-alarm world (closed_lock_red_support): offline FakeTracker,
real `fa gate tick --no-start`, `fa orch inbox --json`, and main's recorded acceptance results in
the machine-wide baseline cache ``<state parent>/baselines/<key>.json``.

Addition here: ``land_member`` lands the closed issue's work on ``origin/main`` the way the merge
owner's batch train does on real main (11-09: 1e86ee9c "Merge member #2675", 93aae973 "Merge
member #2878"): the member branch commit "<title> (#N)" is made on top of the batch base, and main
gets a merge commit whose first parent is the batch base, whose second parent is that member
commit, with subject ``Merge member #N`` and trailer ``Foragent-Member: #N <member head>
<started_at>``. Main commits from before that merge commit do not contain the issue's merge; the
merge commit and every main commit after it do.

The 11-09 false alarm: the batch base was measured (its locked tests red, since the member had not
landed yet), then the member merged and the issue closed, and the next gate pass raised one request
per locked test naming the batch base as first red.
"""

from datetime import timedelta

from conftest import git_command

from closed_lock_red_support import CLOSED_ISSUE


def land_member(case, issue=CLOSED_ISSUE, *, minutes=20):
    """Merge the issue's member branch into main on top of the current tip; returns the merge SHA.

    The member commit's parent is main's current tip (the batch base)."""
    case.clock[0] += timedelta(minutes=minutes)
    at = int((case.clock[0] - timedelta(minutes=30)).timestamp())
    base = case.history.tip
    path = f"src/work_{issue}.py"
    content = f"WORK_{issue} = True\n"
    member_message = f"Locked work {issue} (#{issue})"
    started_at = (case.clock[0] - timedelta(hours=1)).isoformat()

    def blob_lines():
        return [f"M 100644 inline {path}\n", f"data {len(content.encode())}\n{content}\n"]

    stream = [f"commit refs/heads/member-{issue}\n",
              f"committer Offline Tests <tests@example.com> {at} +0000\n",
              f"data {len(member_message.encode())}\n{member_message}\n",
              f"from {base}\n", *blob_lines(), "\ndone\n"]
    git_command(case.history.origin, "fast-import", "--quiet", input="".join(stream))
    member = git_command(case.history.origin, "rev-parse", f"refs/heads/member-{issue}")

    message = f"Merge member #{issue}\n\nForagent-Member: #{issue} {member} {started_at}\n"
    stream = ["commit refs/heads/main\n",
              f"committer Offline Tests <tests@example.com> {at + 1} +0000\n",
              f"data {len(message.encode())}\n{message}\n",
              f"from {base}\n", f"merge {member}\n", *blob_lines(), "\ndone\n"]
    git_command(case.history.origin, "fast-import", "--quiet", input="".join(stream))
    case.history.tip = git_command(case.history.origin, "rev-parse", "refs/heads/main")
    git_command(case.root, "fetch", "--quiet", "origin")
    assert git_command(case.root, "rev-parse", "origin/main") == case.history.tip
    assert git_command(case.root, "rev-parse", f"{case.history.tip}^1") == base
    assert git_command(case.root, "rev-parse", f"{case.history.tip}^2") == member
    return case.history.tip
