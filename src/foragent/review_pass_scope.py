"""A reviewer PASS as a boundary: later reviews of the same issue body judge what changed since.

Facts live in the journal: review events carry their body hash, a PASS also its reviewed tree, and
publication binds a local review's tree to its commit. The reviewer alone judges each finding.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
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


def review_facts(n: int, ctx: RunContext, git: GitRepo | None, passed: bool) -> dict[str, str]:
    """The fields one review event records: its body, and for a real PASS what it reviewed."""
    approval = ctx.store.load_approval(n)
    facts = {} if approval is None else {"body_sha": approval.body_sha}
    if passed and git is not None:
        facts["tree"] = git.staged_tree_sha()
        if git._call(["rev-parse", "HEAD^{tree}"]).strip() == facts["tree"]:
            facts["head"] = git.head_sha()
    return facts


def _passes(n: int, ctx: RunContext) -> list[dict[str, Any]]:
    return [event for event in orch_events.events(ctx.store, "review") if event.get("issue") == n
            and event.get("verdict") == "PASS" and event.get("source") == "reviewer" and "tree" in event]


def _heads(n: int, ctx: RunContext) -> dict[str, str]:
    return {event["tree"]: event["head"] for event in orch_events.events(ctx.store, BOUND) if event.get("issue") == n}


def bind(n: int, ctx: RunContext, git: GitRepo) -> None:
    """Publication committed the index: give the newest reviewed PASS that exact commit."""
    passes = _passes(n, ctx)
    if (passes and "head" not in passes[-1] and passes[-1]["tree"] not in _heads(n, ctx)
            and git._call(["rev-parse", "HEAD^{tree}"]).strip() == passes[-1]["tree"]):
        orch_events.append_event(ctx.store, ctx.now(), n, BOUND, f"PASS round {passes[-1].get('round')} is published",
                                 tree=passes[-1]["tree"], head=git.head_sha())


def _latest_pass(n: int, ctx: RunContext, git: GitRepo) -> tuple[str, Any, str] | None:
    """(head, round, record) of the newest usable PASS for the approved body, if the setting is on."""
    approval = ctx.store.load_approval(n)
    if not ctx.config.review_pass_scope or approval is None:
        return None
    heads = _heads(n, ctx)
    for event in reversed(_passes(n, ctx)):
        head = event.get("head") or heads.get(event["tree"])
        if head and event.get("body_sha") == approval.body_sha:
            path = ctx.store.root / "runs" / f"issue-{n}" / f"review-{event.get('round')}.txt"
            try:
                git._call(["cat-file", "-e", f"{head}^{{commit}}"])
                return head, event.get("round"), path.read_text(encoding="utf-8", errors="replace")
            except (GitError, OSError):
                continue
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


def for_review(n: int, ctx: RunContext, git: GitRepo, base_ref: str) -> Boundary | None:
    """The applicable boundary for a review of this index, or None for today's review."""
    passed = _latest_pass(n, ctx, git)
    if passed is None:
        return None
    head, number, record = passed
    expected = _expected_tree(git, head, base_ref)
    since = git._call(["diff", "--cached", "--name-only", "--no-renames", "-z", expected]).split("\0")
    paths = sorted({*git.changed_paths(base_ref), *filter(None, since)})
    # Read from the base ref: a branch cannot grant itself an exemption.
    matched = risks.matching(risks.load_at(git, base_ref), paths)
    exempt = [risk for risk in matched if risk.get("pass_scope_exempt") is True]
    full = [path for path in paths if any(risks.touched([path], risk["paths"]) for risk in exempt)]
    facts = f"A reviewer gave PASS to head {head} in round {number}, and the issue body is the same as at that PASS."
    kept = cuts.head(record, 8000, cuts.issue_dir(ctx.store.root, n), f"review-pass-{number}.txt")
    evidence = f"Actual review record of that PASS (verbatim):\n{kept}"
    notes = risks.review_notes(matched, paths)
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


def keep_notes(n: int, ctx: RunContext, head: str, notes: tuple[str, ...]) -> None:
    """Put a scoped review's NOTE lines on the issue, once each, so they stay findable after the PR closes."""
    posted = "\n".join(comment.body for comment in ctx.tracker.list_comments(n)) if notes else ""
    lines = [line for line in (f"NOTE: {note}" for note in notes) if line not in posted]
    if lines:
        ctx.tracker.comment(n, "\n".join([f"Review notes on code unchanged since PASS head {head} (nonblocking):", *lines]))
