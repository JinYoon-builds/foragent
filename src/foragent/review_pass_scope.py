"""A reviewer PASS as a boundary: later reviews of the same issue body judge what changed since.

Facts live in the journal: review events carry their body hash, a PASS also its reviewed tree, and
publication binds a local review's tree to its commit. The reviewer alone judges each finding.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from foragent import cuts, orch_events, risks
from foragent.git import GitError

if TYPE_CHECKING:
    from foragent.git import GitRepo
    from foragent.runner import RunContext

BOUND = "review-pass-head"


@dataclass(frozen=True)
class Boundary:
    head: str
    notes: str  # risk-map notes for the whole change and everything since the PASS
    section: str  # the reviewer's facts and scope
    diff: str  # post-PASS diff, plus the whole change of matched pass_scope_exempt paths


def review_facts(n: int, ctx: RunContext, git: GitRepo | None) -> dict[str, str]:
    """The fields one review event records: its body and the tree it reviewed (any verdict).

    A FAIL's tree matters once the orchestrator releases the hold that reviewed it (r1288)."""
    approval = ctx.store.load_approval(n)
    facts = {} if approval is None else {"body_sha": approval.body_sha}
    if git is not None:
        facts["tree"] = git.staged_tree_sha()
        if git._call(["rev-parse", "HEAD^{tree}"]).strip() == facts["tree"]:
            facts["head"] = git.head_sha()
    return facts


def _passes(n: int, ctx: RunContext) -> list[dict[str, Any]]:
    """Reviewer PASS events: the only rounds that let a head skip another review."""
    return [event for event in orch_events.events(ctx.store, "review") if event.get("issue") == n
            and event.get("verdict") == "PASS" and event.get("source") == "reviewer" and "tree" in event]


def _heads(n: int, ctx: RunContext) -> dict[str, str]:
    return {event["tree"]: event["head"] for event in orch_events.events(ctx.store, BOUND) if event.get("issue") == n}


def bind(n: int, ctx: RunContext, git: GitRepo) -> None:
    """Publication committed the index: give the newest reviewed tree that exact commit."""
    reviewed = [event for event in orch_events.events(ctx.store, "review")
                if event.get("issue") == n and "tree" in event]
    if (reviewed and "head" not in reviewed[-1] and reviewed[-1]["tree"] not in _heads(n, ctx)
            and git._call(["rev-parse", "HEAD^{tree}"]).strip() == reviewed[-1]["tree"]):
        event = reviewed[-1]
        orch_events.append_event(ctx.store, ctx.now(), n, BOUND,
                                 f"{event.get('verdict')} round {event.get('round')} is published",
                                 tree=event["tree"], head=git.head_sha())


def _boundaries(n: int, ctx: RunContext) -> list[tuple[dict[str, Any], str]]:
    """Review events that end the reviewed scope, oldest first, each with who set it.

    A reviewer PASS; the orchestrator's PASS on the head it resolved; and the head the last FAIL
    round before a releasing review-decision answer reviewed (the hold the orchestrator released).
    """
    from foragent import review_scoring

    decided = review_scoring.decided_at(n, ctx)
    events = [event for event in orch_events.events(ctx.store, "review") if event.get("issue") == n]
    released = None
    if decided is not None:
        released = next((event for event in reversed(events) if event.get("verdict") == "FAIL"
                         and datetime.fromisoformat(event["at"]) <= decided), None)
    found = []
    for event in events:
        if event.get("verdict") == "PASS" and event.get("source") == "reviewer" and "tree" in event:
            found.append((event, "reviewer"))
        elif event.get("verdict") == "PASS" and event.get("source") == "orchestrator" and "head" in event:
            found.append((event, "orchestrator"))
        elif event is released and "tree" in event:
            found.append((event, "released"))
    return found


def _latest_pass(n: int, ctx: RunContext, git: GitRepo) -> tuple[str, Any, str, str] | None:
    """(head, round, record, kind) of the newest usable boundary for the approved body, if the setting is on."""
    approval = ctx.store.load_approval(n)
    if not ctx.config.review_pass_scope or approval is None:
        return None
    heads = _heads(n, ctx)
    for event, kind in reversed(_boundaries(n, ctx)):
        head = event.get("head") or heads.get(event.get("tree"))
        if head and event.get("body_sha") == approval.body_sha:
            path = ctx.store.root / "runs" / f"issue-{n}" / f"review-{event.get('round')}.txt"
            try:
                git._call(["cat-file", "-e", f"{head}^{{commit}}"])
            except GitError:
                continue
            try:
                record = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                if kind == "reviewer":
                    continue
                record = "(no reviewer record: the orchestrator gave this verdict)"
            return head, event.get("round"), record, kind
    return None


def applies(n: int, ctx: RunContext, git: GitRepo) -> bool:
    return _latest_pass(n, ctx, git) is not None


def anchor_body(n: int, ctx: RunContext) -> str | None:
    """Once a PASS exists, a confirmation anchor must belong to the approved body; before that, any round may."""
    approval = ctx.store.load_approval(n)
    return approval.body_sha if ctx.config.review_pass_scope and approval and _passes(n, ctx) else None


def _expected_tree(git: GitRepo, head: str, base_ref: str) -> str:
    """The PASS head with the integrated main merged in: main-only code folds in, conflicts stay as markers."""
    try:
        return git._call(["merge-tree", "--write-tree", head, git._own_base(base_ref)]).split("\n", 1)[0].strip()
    except GitError as exc:
        cause = exc.__cause__
        if isinstance(cause, subprocess.CalledProcessError) and cause.returncode == 1 and cause.output:
            return str(cause.output).split("\n", 1)[0].strip()  # conflicted hunks keep their markers
        return head  # no merge possible: compare with the PASS head, which only widens the input


def _marker_regions(text: str) -> list[tuple[int, int]]:
    """1-based inclusive line ranges of conflict blocks, from `<<<<<<< ` to `>>>>>>> `."""
    regions, start = [], None
    for number, line in enumerate(text.splitlines(), 1):
        if line.startswith("<<<<<<< ") and start is None:
            start = number
        elif line.startswith(">>>>>>> ") and start is not None:
            regions.append((start, number))
            start = None
    return regions


def _within_conflicts(git: GitRepo, expected: str, head: str) -> bool:
    """True when the index differs from the plain merge only inside its conflict blocks.

    Any other edit (a line outside a block, a new, deleted or binary file, a mode change) or an unknown
    plain merge means the resolution is not confined, so the review and re-check stay whole (#2310).
    """
    if expected == head:
        return False  # merge-tree failed: the plain merge, and so the conflict blocks, are unknown
    text = git._call(["diff", "--cached", "-U0", "--no-ext-diff", "--no-textconv", "--no-renames", expected])
    regions: list[tuple[int, int]] = []
    hunks = 0
    for line in text.splitlines():
        if line.startswith("diff --git "):
            path = line.split(" b/", 1)[-1]
            try:
                regions = _marker_regions(git._call(["show", f"{expected}:{path}"]))
            except GitError:
                return False
        elif line.startswith(("new file", "deleted file", "old mode", "new mode", "Binary files", "GIT binary")):
            return False
        elif line.startswith("@@ "):
            old = line.split(" ", 2)[1][1:].split(",")
            start, count = int(old[0]), int(old[1]) if len(old) > 1 else 1
            hunks += 1
            if count:
                inside = any(low <= start and start + count - 1 <= high for low, high in regions)
            else:  # a pure insertion after line `start`
                inside = any(low <= start < high for low, high in regions)
            if not inside:
                return False
    return hunks > 0


def for_review(n: int, ctx: RunContext, git: GitRepo, base_ref: str) -> Boundary | None:
    """The applicable boundary for a review of this index, or None for today's review."""
    passed = _latest_pass(n, ctx, git)
    if passed is None:
        return None
    head, number, record, kind = passed
    expected = _expected_tree(git, head, base_ref)
    since = git._call(["diff", "--cached", "--name-only", "--no-renames", "-z", expected]).split("\0")
    paths = sorted({*git.changed_paths(base_ref), *filter(None, since)})
    # Read from the base ref: a branch cannot grant itself an exemption.
    matched = risks.matching(risks.load_at(git, base_ref), paths)
    exempt = [risk for risk in matched if risk.get("pass_scope_exempt") is True]
    full = [path for path in paths if any(risks.touched([path], risk["paths"]) for risk in exempt)]
    facts = {
        "reviewer": f"A reviewer gave PASS to head {head} in round {number}",
        "orchestrator": f"The orchestrator resolved head {head} as PASS",
        "released": (f"The orchestrator released the review-decision hold on head {head} (reviewed in round "
                     f"{number}), which counts as a PASS here"),
    }[kind] + ", and the issue body is the same as at that PASS."
    kept = cuts.head(record, 8000, cuts.issue_dir(ctx.store.root, n), f"review-pass-{number}.txt")
    evidence = f"Actual review record of that round (verbatim):\n{kept}"
    notes = risks.review_notes(matched, paths)
    if _within_conflicts(git, expected, head):
        # A resolution confined to the conflict blocks: the notes name only what it and exempt paths
        # cover, since every PR path there would ask for the whole change again (#2310).
        scoped = sorted({*filter(None, since), *full})
        notes = risks.review_notes(risks.matching(risks.load_at(git, base_ref), scoped), scoped)
    diff = ["diff", "--cached", "--no-ext-diff", "--no-textconv", "--no-renames"]
    if full:
        entries = "; ".join(", ".join(risk["paths"]) for risk in exempt)
        named = ", ".join(full)
        since_pass = git._call([*diff, expected, "--", ".", *(f":(exclude,literal){path}" for path in full)])
        whole = git._call([*diff, git._own_base(base_ref), "--", *(f":(literal){path}" for path in full)])
        return Boundary(head, notes, "\n".join([
            "Earlier PASS boundary with exempt paths (setting review_pass_scope: true).", facts,
            f"This change touches pass_scope_exempt risk-map entries ({entries}). Exempt paths: {named}. "
            "On those paths the PASS does not narrow this review: the diff below shows their whole change in this "
            "PR, and a real defect in their unchanged code can still block.",
            "Everywhere else, limit blocking findings to code changed after that PASS. For other paths the diff "
            "below runs from the PASS head, with current main merged in, to the current tree. Code that arrived "
            "from main only by integrating it is left out, even inside a file this PR changes, because its own "
            "issue already reviewed it; conflict resolutions and new commits of this PR count as changes and can "
            "block. CI checks the combined code. Any instruction here to review all changed files means this diff.",
            "Outside the exempt paths, a defect confined to unchanged code does not block: write it as a "
            "`NOTE: <path:line> — <defect>` line. A defect a later change causes or exposes belongs to that later "
            "change. You judge each finding.",
            evidence]), since_pass + whole)
    return Boundary(head, notes, "\n".join([
        "Earlier PASS boundary (setting review_pass_scope: true; no pass_scope_exempt risk-map entry matches "
        "this change).", facts,
        "Limit blocking findings to code changed after that PASS. The diff below runs from the PASS head, with "
        "current main merged in, to the current tree. Code that arrived from main only by integrating it is left "
        "out, even inside a file this PR changes, because its own issue already reviewed it; conflict resolutions "
        "and new commits of this PR count as changes and can block. CI checks the combined code. "
        "Any instruction here to review all changed files means this diff.",
        "A defect confined to unchanged code does not block: write it as a `NOTE: <path:line> — <defect>` line. "
        "A defect a later change causes or exposes belongs to that later change. You judge each finding.",
        evidence]), git._call(["diff", "--cached", "--no-ext-diff", "--no-textconv", expected]))


def recheck_paths(n: int, ctx: RunContext, git: GitRepo, base_ref: str) -> list[str] | None:
    """Paths a conflict resolution after a PASS or main's new commits touched; None means re-check all.

    None comes without a PASS, or when the resolution edits anything outside the conflict blocks.

    The resolution is the index against the plain merge of the PASS head with main; main's new commits
    run from that head's merge base with main. Tests related only to other PR paths stay with PR CI.
    """
    passed = _latest_pass(n, ctx, git)
    if passed is None:
        return None
    head = passed[0]
    main = git._call(["rev-parse", f"{base_ref}^{{commit}}"]).strip()
    expected = _expected_tree(git, head, base_ref)
    if not _within_conflicts(git, expected, head):
        return None
    resolved = git._call(["diff", "--cached", "--name-only", "--no-renames", "-z", expected])
    try:
        fork = git._call(["merge-base", head, main]).strip()
        moved = git._call(["diff", "--name-only", "--no-renames", "-z", fork, main])
    except GitError:
        return None  # no shared history: the whole related set stays local
    return sorted({*filter(None, resolved.split("\0")), *filter(None, moved.split("\0"))})


def keep_notes(n: int, ctx: RunContext, head: str, notes: tuple[str, ...]) -> None:
    """Put a scoped review's NOTE lines on the issue, once each, so they stay findable after the PR closes."""
    posted = "\n".join(comment.body for comment in ctx.tracker.list_comments(n)) if notes else ""
    lines = [line for line in (f"NOTE: {note}" for note in notes) if line not in posted]
    if lines:
        ctx.tracker.comment(n, "\n".join([f"Review notes on code unchanged since PASS head {head} (nonblocking):", *lines]))
