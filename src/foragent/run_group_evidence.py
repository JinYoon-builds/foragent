"""Whether a follower may count an earlier joint group check as its own evidence (group-evidence).

A joint result is spendable only when everything it assumed still holds:

- the group verdict is exactly ``passed`` (a ``failed`` or ``void`` group never is);
- the follower's PR head at this final read is the head that was checked;
- current main descends from the captured group base through exactly the earlier
  members' merges, first parent by first parent: one commit per earlier member,
  each with the expected previous tip as parent and that member's checked tree.
  Any other commit (an outsider, including one that reverts itself, or one that
  lands inside a member's merge call) makes the interval unproven.

``void_reason`` is the single predicate; ``None`` means usable. Until group reuse
exists, ``note`` only records the decision in the follower's own log and one
journal event, and the follower keeps its ordinary one-PR verification.

Provenance is recorded by each member itself: right after its own ``merge_pr``
succeeds, ``record_merge`` observes the new main and appends one ``group-merge``
event naming the commit it can attribute to its merge (parent = the tip it read
under the merge lock, tree = that tip merged with its exact head, and no other
commit in between). A commit is never attributed by content alone.

State ownership: reads ``group-check`` / ``group-merge`` journal events and git;
each runner writes only its own log and its own ``group-evidence`` /
``group-merge`` events.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from foragent.git import GitError
from foragent.tracker import TrackerError

if TYPE_CHECKING:
    from foragent.runner import RunContext


def latest(ctx: RunContext, n: int) -> tuple[dict, int] | None:
    """The newest ``group-check`` event naming ``n`` as a follower, and ``n``'s position in it."""
    for event in reversed(_events(ctx, "group-check")):
        members = event.get("members")
        if not isinstance(members, list):
            continue
        numbers = [member.get("n") if isinstance(member, dict) else None for member in members]
        if n in numbers[1:]:
            return event, numbers.index(n)
    return None


def void_reason(ctx: RunContext, n: int, head_sha: str, base_sha: str,
                found: tuple[dict, int] | None = None) -> str | None:
    """Why ``n`` may not spend the joint result on main ``base_sha``; ``None`` when it may."""
    found = found if found is not None else latest(ctx, n)
    if found is None:
        return "no joint result names this run"
    event, index = found
    verdict = event.get("verdict")
    if verdict != "passed":
        return f"group verdict is {verdict or 'unknown'}"
    members = event["members"]
    if members[index].get("head_sha") != head_sha:
        return "the checked head is not this run's head"
    try:
        current = _current_head(ctx, n)
    except TrackerError as exc:
        if exc.is_rate_limit:
            raise
        return f"PR head unreadable: {exc}"
    if current != head_sha:
        return "the PR head changed after the joint check"
    group_base = event.get("base_sha")
    trees = [member.get("tree") for member in members[:index]]
    if not isinstance(group_base, str) or not all(isinstance(tree, str) and tree for tree in trees):
        return "the joint result recorded no merge provenance"
    try:
        lines = ctx.git._call(["log", "--first-parent", "--reverse", "--format=%H %T %P",
                               f"{group_base}..{base_sha}"]).splitlines()
    except (GitError, OSError) as exc:
        return f"main history unreadable: {exc}"
    commits = [line.split() for line in lines if line.strip()]
    if len(commits) != len(trees):
        return (f"main moved by {len(commits)} commits since the group base; "
                f"expected exactly the {len(trees)} earlier member merges")
    merges = _recorded_merges(ctx)
    tip = group_base
    for (sha, tree, *parents), expected, member in zip(commits, trees, members):
        if not parents or parents[0] != tip or tree != expected:
            return f"commit {sha} is not #{member.get('n')}'s checked merge"
        # Matching content is not provenance: the commit must be the one that member
        # itself observed as its own merge of the exact checked head.
        if merges.get((member.get("n"), member.get("head_sha"))) != (sha, tip, tree):
            return f"commit {sha} has no recorded merge by #{member.get('n')} of its checked head"
        tip = sha
    if tip != base_sha:
        return "main does not end at the earlier members' merges"
    return None


def note(n: int, ctx: RunContext, head_sha: str, base_sha: str) -> str | None:
    """Record whether ``n`` could spend an earlier joint result; return the void reason, if any."""
    from foragent import orch_events
    from foragent.run_checks import _append

    found = latest(ctx, n)
    if found is None:
        return None
    reason = void_reason(ctx, n, head_sha, base_sha, found)
    decision = "usable" if reason is None else f"void ({reason})"
    _append(ctx.store.log_path(n), f"\nGroup evidence on {base_sha}: {decision}; running the ordinary check")
    orch_events.append_event(ctx.store, ctx.now(), n, "group-evidence", f"group evidence {decision}",
                             base_sha=base_sha, head_sha=head_sha, usable=reason is None, reason=reason,
                             group_base=found[0].get("base_sha"))
    return reason


def record_merge(n: int, ctx: RunContext, target: str, pre_tip: str, head_sha: str) -> None:
    """After ``n``'s own successful merge, record the main commit that is that merge.

    Only for a run named by a joint result that has later members (they are the
    only readers). Recorded as unattributed when anything else is in the way: a
    commit before it (an outsider racing the final read), a different tree, or an
    unreadable history. Never raises; a missing record voids reuse, nothing more.
    """
    from foragent import orch_events
    from foragent.run_checks import _append

    if not _has_followers(ctx, n, head_sha):
        return
    commit = reason = None
    try:
        ctx.git.fetch("origin", target)
        tip = ctx.git.ref_sha(f"origin/{target}")
        expected = ctx.git._call(["merge-tree", "--write-tree", pre_tip, head_sha]).splitlines()[0].strip()
        lines = ctx.git._call(["log", "--first-parent", "--reverse", "--format=%H %T %P",
                               f"{pre_tip}..{tip}"]).splitlines()
        commits = [line.split() for line in lines if line.strip()]
        first = commits[0] if commits else None
        if first is None or len(first) < 3 or first[2] != pre_tip or first[1] != expected:
            reason = "the first main commit after the merge is not this merge"
        elif any(other[1] == expected for other in commits[1:]):
            reason = "more than one main commit could be this merge"
        else:
            commit = {"sha": first[0], "parent": pre_tip, "tree": expected}
    except (GitError, OSError, IndexError) as exc:
        reason = f"main unreadable after the merge: {str(exc).strip() or type(exc).__name__}"
    _append(ctx.store.log_path(n), f"\nObserved merge commit: {commit['sha'] if commit else f'none ({reason})'}")
    orch_events.append_event(ctx.store, ctx.now(), n, "group-merge",
                             f"merge of {head_sha} " + (f"is {commit['sha']}" if commit else f"unattributed ({reason})"),
                             head_sha=head_sha, commit=commit, reason=reason)


def _events(ctx: RunContext, kind: str) -> list[dict]:
    path = ctx.store.root / "orch-events.jsonl"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except (FileNotFoundError, NotADirectoryError):
        return []
    events = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("kind") == kind:
            events.append(event)
    return events


def _has_followers(ctx: RunContext, n: int, head_sha: str) -> bool:
    for event in _events(ctx, "group-check"):
        members = event.get("members") if event.get("verdict") == "passed" else None
        if not isinstance(members, list):
            continue
        keys = [(m.get("n"), m.get("head_sha")) if isinstance(m, dict) else None for m in members]
        if (n, head_sha) in keys[:-1]:
            return True
    return False


def _recorded_merges(ctx: RunContext) -> dict[tuple, tuple]:
    """(issue, merged head) -> (commit, parent, tree), from each member's own record."""
    merges = {}
    for event in _events(ctx, "group-merge"):
        commit = event.get("commit")
        if isinstance(commit, dict):
            merges[(event.get("issue"), event.get("head_sha"))] = (
                commit.get("sha"), commit.get("parent"), commit.get("tree"))
    return merges


def _current_head(ctx: RunContext, n: int) -> str | None:
    run = ctx.store.get_run(n)
    if run is None or not run.branch:
        return None
    pr = ctx.tracker.find_pr(run.branch)
    return None if pr is None else ctx.tracker.pr_head_sha(pr)
