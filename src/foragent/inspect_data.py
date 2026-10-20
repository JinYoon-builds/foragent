"""Arrange the gate's GitHub records into a human inbox without new decisions."""

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import re

from foragent.config import Config
from foragent.labels import NEEDS_HUMAN
from foragent.reasons import GROUP
from foragent.state import StateStore
from foragent.tracker import Comment, Tracker


GROUPS = ("decision", "reapprove", "review", "human-check", "try", "tech-failure")
ACTIONABLE = frozenset(GROUPS) - {"try"}
DECISION_MARKER = "<!-- foragent:decision -->"
RETRY_MARKER = "<!-- foragent:retry -->"
HEAD_PREFIX = "foragent/issue-"


@dataclass
class InspectContext:
    tracker: Tracker
    store: StateStore
    repo: str
    config: Config
    now: Callable[[], datetime]


@dataclass
class Item:
    group: str
    number: int
    title: str
    url: str
    reason: str | None
    summary: str
    details: str
    pr: int | None
    answered: bool


@dataclass
class Inbox:
    items: list[Item]
    counts: dict[str, int]
    since: str | None


def comment_key(comment: Comment) -> tuple[datetime, int]:
    return datetime.fromisoformat(comment.created_at), comment.id


def latest_flag(comments: list[Comment]) -> tuple[Comment, str] | None:
    for comment in sorted(comments, key=comment_key, reverse=True):
        match = re.fullmatch(
            r"<!-- foragent:needs-human reason=([a-z-]+) -->", comment.body.splitlines()[0]
        ) if comment.body else None
        if match:
            return comment, match[1]
    return None


def has_marker(comment: Comment, marker: str) -> bool:
    return bool(comment.body) and comment.body.splitlines()[0] == marker


def _gate_text(comment: Comment) -> tuple[str, str]:
    content = comment.body.split("\n", 1)[1].strip() if "\n" in comment.body else ""
    first, _, rest = content.partition("\n")
    summary = first[2:-2] if first.startswith("**") and first.endswith("**") else first
    return summary or "Needs a human", rest.strip()[:4000]


def _human_checks(body: str) -> str:
    checks = []
    in_section = False
    level = 0
    fence = None
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            fence = None if fence == marker else marker if fence is None else fence
            continue
        if fence:
            continue
        heading = re.fullmatch(r"(#{1,6})\s+(.+?)(?:\s+#+)?\s*", line)
        if heading:
            if in_section and len(heading[1]) <= level:
                in_section = False
            if heading[2].strip() == "Needs a human check":
                in_section, level = True, len(heading[1])
        if in_section and re.match(r"\s*[-*+]\s+\[ \]\s+\S", line):
            checks.append(line.strip())
    return "\n".join(checks)[:4000]


def collect(ctx: InspectContext) -> Inbox:
    since = ctx.store.load_inspect().since or (ctx.now() - timedelta(days=7)).astimezone(UTC).isoformat()
    cutoff = datetime.fromisoformat(since)
    issues = {issue.number: issue for issue in ctx.tracker.list_open_issues()}
    open_prs = sorted(ctx.tracker.list_prs("open", HEAD_PREFIX), key=lambda pr: pr.number)
    linked = {}
    for pr in open_prs:
        if match := re.fullmatch(r"foragent/issue-(\d+)", pr.head):
            linked.setdefault(int(match[1]), pr)
    items = []
    for issue in issues.values():
        if NEEDS_HUMAN not in issue.labels:
            continue
        comments = ctx.tracker.list_comments(issue.number)
        flag = latest_flag(comments)
        reason, group = None, "decision"
        summary, details = "Needs a human (no Foragent comment found)", ""
        answered = False
        if flag:
            comment, reason = flag
            group = GROUP.get(reason, "decision")
            summary, details = _gate_text(comment)
            answered = any(
                has_marker(answer, DECISION_MARKER) and comment_key(answer) > comment_key(comment)
                for answer in comments
            )
        pr = linked.get(issue.number)
        url = f"https://github.com/{ctx.repo}/issues/{issue.number}"
        if group == "review" and pr:
            url = pr.url or f"https://github.com/{ctx.repo}/pull/{pr.number}"
        items.append(Item(
            group, issue.number, issue.title, url, reason, summary, details,
            pr.number if pr else None, answered,
        ))

    for pr in open_prs:
        match = re.fullmatch(r"foragent/issue-(\d+)", pr.head)
        if not match:
            continue
        number = int(match[1])
        if number in issues and NEEDS_HUMAN in issues[number].labels:
            continue
        items.append(Item(
            "review", number, pr.title, pr.url or f"https://github.com/{ctx.repo}/pull/{pr.number}",
            None, "PR awaits human review", pr.body.strip()[:4000], pr.number, False,
        ))

    merged_prs = [pr for pr in ctx.tracker.list_prs("merged", HEAD_PREFIX) if pr.merged_at is not None]
    merged_prs.sort(key=lambda pr: (datetime.fromisoformat(pr.merged_at), pr.number), reverse=True)
    tried_issues = set()
    for pr in merged_prs:
        match = re.fullmatch(r"foragent/issue-(\d+)", pr.head)
        if not match or pr.merged_at is None or datetime.fromisoformat(pr.merged_at) <= cutoff:
            continue
        number = int(match[1])
        url = pr.url or f"https://github.com/{ctx.repo}/pull/{pr.number}"
        if number not in tried_issues:
            items.append(Item(
                "try", number, pr.title, url, None, f"Merged in PR #{pr.number}; try it",
                "", pr.number, False,
            ))
            tried_issues.add(number)
        if checks := _human_checks(pr.body):
            items.append(Item(
                "human-check", number, pr.title, url, None, "Unchecked human checks",
                checks, pr.number, False,
            ))
    items.sort(key=lambda item: (GROUPS.index(item.group), item.number, item.pr or 0))
    counts = Counter(item.group for item in items)
    return Inbox(items, {group: counts[group] for group in GROUPS}, since)


def pending_signature(inbox: Inbox) -> str:
    pending = sorted(
        (item.group, item.number, item.reason or "")
        for item in inbox.items if item.group in ACTIONABLE
    )
    return hashlib.sha256(json.dumps(pending, separators=(",", ":")).encode()).hexdigest()


def counts_line(inbox: Inbox) -> str:
    names = ("Decision", "Re-approve", "Review", "Human check", "Try", "Tech failure")
    return " · ".join(
        f"{name} {inbox.counts[group]}" for group, name in zip(GROUPS, names)
        if inbox.counts.get(group, 0)
    ) or "No items."
