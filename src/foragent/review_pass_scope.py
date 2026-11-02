"""A real reviewer PASS as a boundary: later reviews of the same issue body judge what changed since.

The journal holds the facts: each review first records the issue-body hash it runs under, so an
interrupted review still counts, and a PASS also carries the tree it reviewed. A local review runs before publication commits, so
publication appends the commit once that tree has one. Everything else is derived on read, so a
restart keeps the boundary, a body edit drops it and the repository setting ignores it. The
reviewer alone judges whether a finding blocks or is a NOTE.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from foragent import cuts, orch_events, risks
from foragent.git import GitError

if TYPE_CHECKING:
    from foragent.git import GitRepo
    from foragent.runner import RunContext
    from foragent.state import StateStore

STARTED = "review-started"
BOUND = "review-pass-head"
RECORD_LIMIT = 8000


@dataclass(frozen=True)
class Boundary:
    head: str
    notes: str  # risk-map notes for the whole change and everything since the PASS
    section: str  # the reviewer's facts and scope
    diff: str | None  # the post-PASS diff; None keeps today's diff and task for an exempt change


def start(n: int, ctx: RunContext, round_number: int, body_sha: str | None) -> None:
    """Record the body before the reviewer runs: no later interruption can lose this fact."""
    if body_sha is None:
        approval = ctx.store.load_approval(n)
        body_sha = None if approval is None else approval.body_sha
    if body_sha is not None:
        orch_events.append_event(ctx.store, ctx.now(), n, STARTED, f"Review round {round_number} started",
                                 round=round_number, body_sha=body_sha)


def review_facts(n: int, ctx: RunContext, git: GitRepo | None, passed: bool,
                 round_number: int | None = None) -> dict[str, str]:
    """The fields one review event records: its body, and for a real PASS what it reviewed.

    The body is the one the round started under, even if the approval changed while it ran.
    """
    started = [event for event in orch_events.events(ctx.store, STARTED)
               if event.get("issue") == n and event.get("round") == round_number]
    approval = ctx.store.load_approval(n)
    body_sha = started[-1]["body_sha"] if started else None if approval is None else approval.body_sha
    facts = {} if body_sha is None else {"body_sha": body_sha}
    if passed and git is not None:
        tree = git.staged_tree_sha()
        facts["tree"] = tree
        if _tree_of(git, "HEAD") == tree:
            facts["head"] = git.head_sha()
    return facts


def _tree_of(git: GitRepo, commit: str) -> str:
    return git._call(["rev-parse", f"{commit}^{{tree}}"]).strip()


def _reviews(store: StateStore, n: int) -> list[dict[str, Any]]:
    """Every review's start and result, in journal order."""
    return [event for event in orch_events.events(store, "review", STARTED) if event.get("issue") == n]


def _is_pass(event: dict[str, Any]) -> bool:
    return event.get("verdict") == "PASS" and event.get("source") == "reviewer" and "tree" in event


def bind(n: int, ctx: RunContext, git: GitRepo) -> None:
    """Publication committed the index: give the newest reviewed PASS that exact commit."""
    passes = [event for event in _reviews(ctx.store, n) if _is_pass(event)]
    if not passes or "head" in passes[-1]:
        return
    latest = passes[-1]
    if (latest.get("round"), latest["tree"]) in _bound(ctx.store, n) or _tree_of(git, "HEAD") != latest["tree"]:
        return
    head = git.head_sha()
    orch_events.append_event(ctx.store, ctx.now(), n, BOUND, f"PASS round {latest.get('round')} is {head}",
                             round=latest.get("round"), tree=latest["tree"], head=head)


def _bound(store: StateStore, n: int) -> dict[tuple[Any, Any], str]:
    return {(event.get("round"), event.get("tree")): event.get("head")
            for event in orch_events.events(store, BOUND) if event.get("issue") == n}


def _record(store: StateStore, n: int, number: Any) -> str:
    """The saved reviewer text of a PASS round; empty when that record no longer says PASS."""
    from foragent.run_review import parse_review

    path = store.root / "runs" / f"issue-{n}" / f"review-{number}.txt"
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return raw if parse_review(raw).verdict == "PASS" else ""


def _exists(git: GitRepo, commit: str) -> bool:
    try:
        git._call(["cat-file", "-e", f"{commit}^{{commit}}"])
    except GitError:
        return False
    return True


def _latest_pass(n: int, ctx: RunContext, git: GitRepo) -> tuple[str, Any, str] | None:
    """(head, round, record) of the applicable PASS; any review under another body ends the search."""
    if not ctx.config.review_pass_scope:
        return None
    approval = ctx.store.load_approval(n)
    if approval is None:
        return None
    bound = _bound(ctx.store, n)
    for event in reversed(_reviews(ctx.store, n)):
        if event.get("body_sha", approval.body_sha) != approval.body_sha:
            return None
        if not _is_pass(event):
            continue
        head = event.get("head") or bound.get((event.get("round"), event["tree"]))
        record = _record(ctx.store, n, event.get("round")) if head else ""
        if record and _exists(git, head):
            return head, event.get("round"), record
    return None


def applies(n: int, ctx: RunContext, git: GitRepo) -> bool:
    return _latest_pass(n, ctx, git) is not None


def stale_anchor_round(n: int, ctx: RunContext, *, boundary: bool) -> int | None:
    """The last round that can no longer anchor a confirmation, once a PASS was recorded.

    A review under another body ends every older anchor, even after the text is restored. A
    published PASS ends the older anchors when its boundary is lost, so the review falls back to
    full; an exempt change still has its boundary and keeps today's anchor. Before any PASS (or
    with the setting off) every saved round stays eligible, as always.
    """
    reviews = _reviews(ctx.store, n)
    approval = ctx.store.load_approval(n)
    if not ctx.config.review_pass_scope or approval is None or not any(map(_is_pass, reviews)):
        return None
    bound = _bound(ctx.store, n)
    for event in reversed(reviews):
        published = _is_pass(event) and (event.get("head") or (event.get("round"), event["tree"]) in bound)
        if event.get("body_sha", approval.body_sha) != approval.body_sha or (published and not boundary):
            return event.get("round")
    return None


def for_review(n: int, ctx: RunContext, git: GitRepo, base_ref: str) -> Boundary | None:
    """The applicable boundary for a review of this index, or None for today's review."""
    passed = _latest_pass(n, ctx, git)
    if passed is None:
        return None
    head, number, record = passed
    since = [path for path in git._call(["diff", "--cached", "--name-only", "--no-renames", "-z", head]).split("\0")
             if path]
    paths = sorted({*git.changed_paths(base_ref), *since})
    # Read from the base ref: a branch cannot grant itself an exemption.
    matched = risks.matching(risks.load_at(git, base_ref), paths)
    notes = risks.review_notes(matched, paths)
    exempt = [risk for risk in matched if risk.get("pass_scope_exempt") is True]
    evidence = _evidence(n, ctx, number, record)
    if exempt:
        return Boundary(head, notes, _exempt_section(head, number, exempt, paths, evidence), None)
    diff = git._call(["diff", "--cached", "--no-ext-diff", "--no-textconv", head])
    return Boundary(head, notes, _scoped_section(head, number, evidence), diff)


def _evidence(n: int, ctx: RunContext, number: Any, record: str) -> str:
    kept = cuts.head(record, RECORD_LIMIT, cuts.issue_dir(ctx.store.root, n), f"review-pass-{number}.txt")
    return f"Actual review record of that PASS (verbatim):\n{kept}"


def _scoped_section(head: str, number: Any, evidence: str) -> str:
    return "\n".join([
        "Earlier PASS boundary (setting review_pass_scope: true; no pass_scope_exempt risk-map entry "
        "matches this change).",
        f"A reviewer gave PASS to head {head} in round {number}, and the issue body is the same as at that PASS.",
        "Limit blocking findings to code changed after that PASS. The diff below runs from the PASS head to "
        "the current tree: code integrated from main after the PASS counts as later change and can block, "
        "including separate edits in the same file. Any instruction here to review all changed files means this diff.",
        "A defect confined to unchanged code does not block: write it as a `NOTE: <path:line> — <defect>` line. "
        "A defect a later change causes or exposes belongs to that later change. You judge each finding.",
        evidence,
    ])


def _exempt_section(head: str, number: Any, exempt: list[dict[str, Any]], paths: list[str], evidence: str) -> str:
    entries = "\n".join(f"- Paths: {', '.join(risk['paths'])} (changed here: {', '.join(risks.touched(paths, risk['paths']))})"
                        for risk in exempt)
    return "\n".join([
        "Earlier PASS, exempt from its boundary (setting review_pass_scope: true).",
        f"A reviewer gave PASS to head {head} in round {number}, and the issue body is the same as at that PASS.",
        "This change touches these pass_scope_exempt risk-map entries, so that PASS does not narrow this review:",
        entries,
        "This review keeps its usual scope and diff: a real defect in unchanged code can still block.",
        evidence,
    ])


def keep_notes(n: int, ctx: RunContext, head: str, notes: tuple[str, ...]) -> None:
    """Put a scoped review's NOTE lines on the issue too, once each, for reading after the PR closes."""
    if not notes:
        return
    posted = "\n".join(comment.body for comment in ctx.tracker.list_comments(n))
    lines = [line for line in (f"NOTE: {note}" for note in notes) if line not in posted]
    if lines:
        ctx.tracker.comment(n, "\n".join([f"Review notes on code unchanged since PASS head {head} (nonblocking):", *lines]))
