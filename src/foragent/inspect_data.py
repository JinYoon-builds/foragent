"""Arrange the gate's GitHub records into a human inbox without new decisions."""

from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import re

from foragent.config import Config
from foragent.git import GitRepo
from foragent.labels import BLOCKED, NEEDS_HUMAN, issue_lane, lane_display
from foragent.reasons import GROUP
from foragent.state import StateStore
from foragent.tracker import Comment, Tracker
from foragent.trycard import TryCard, parse_try, pr_try_text


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
    repo_root: Path | None = None
    git: GitRepo | None = None


@dataclass(frozen=True)
class PreparedCommand:
    command: str
    output: str
    exit_code: int | None
    timed_out: bool = False
    stderr: str = ""

    @property
    def stdout(self) -> str:
        return self.output


@dataclass(frozen=True)
class Prepared:
    base_sha: str
    results: list[PreparedCommand]


def load_prepared(store: StateStore, number: int) -> Prepared | None:
    try:
        data = json.loads((store.root / "inspect/prepared" / f"{number}.json").read_text(encoding="utf-8"))
        return Prepared(data["base_sha"], [PreparedCommand(**result) for result in data["results"]])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def inbox_payload(inbox: "Inbox") -> dict:
    payload = asdict(inbox)
    for item in payload["items"]:
        if item["prepared"] is not None:
            for result in item["prepared"]["results"]:
                if result["exit_code"] == 0:
                    result.pop("stderr", None)
                else:
                    result["stderr"] = result["stderr"][-1500:]
    return payload


def _try_card(ctx: InspectContext, number: int, body: str) -> TryCard | None:
    path = ctx.store.root / "try" / f"{number}.md"
    text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else pr_try_text(body)
    if text is None or text.strip() == "_not provided by the implementer_":
        return None
    if not path.is_file():
        text = "## Try it\n" + text
    return parse_try(text)


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
    try_card: TryCard | None = None
    prepared: Prepared | None = None
    lane: str = "main"
    lane_display: str = ""


@dataclass
class Inbox:
    items: list[Item]
    counts: dict[str, int]
    since: str | None
    language: str = "en"
    summary: dict = field(init=False)

    def __post_init__(self):
        self.summary = {
            "counts": self.counts.copy(),
            "estimated_minutes": sum(self.counts.get(group, 0) * (3 if group == "review" else 1)
                                     for group in GROUPS),
        }


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
    open_prs = sorted(ctx.tracker.list_prs("open", "foragent/"), key=lambda pr: pr.number)
    linked = {}
    for pr in open_prs:
        if match := re.fullmatch(r"foragent/(?:issue|batch)-(\d+)", pr.head):
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
        match = re.fullmatch(r"foragent/(?:issue|batch)-(\d+)", pr.head)
        if not match:
            continue
        number = int(match[1])
        if number in issues and issues[number].labels & {BLOCKED, NEEDS_HUMAN}:
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
                _try_card(ctx, number, pr.body), load_prepared(ctx.store, number),
            ))
            tried_issues.add(number)
        if checks := _human_checks(pr.body):
            items.append(Item(
                "human-check", number, pr.title, url, None, "Unchecked human checks",
                checks, pr.number, False,
            ))
    for item in items:
        run = ctx.store.get_run(item.number)
        issue = issues.get(item.number)
        item.lane = run.lane if run is not None else issue_lane(issue.labels if issue else (), ctx.config.lanes)
        item.lane_display = lane_display(ctx.config, item.lane, ctx.config.language)
    items.sort(key=lambda item: (GROUPS.index(item.group), item.number, item.pr or 0))
    counts = Counter(item.group for item in items)
    return Inbox(items, {group: counts[group] for group in GROUPS}, since, ctx.config.language)


def pending_signature(inbox: Inbox) -> str:
    pending = sorted(
        (item.group, item.number, item.reason or "")
        for item in inbox.items if item.group in ACTIONABLE
    )
    return hashlib.sha256(json.dumps(pending, separators=(",", ":")).encode()).hexdigest()


def counts_line(inbox: Inbox, *, detailed: bool = False) -> str:
    names = ("Decision", "Re-approve", "Review", "Human check", "Try", "Tech failure")
    def suffix(group: str) -> str:
        if not detailed:
            return ""
        if group == "try":
            items = [item for item in inbox.items if item.group == "try"]
            return " (prepared)" if items and all(item.prepared is not None for item in items) else " (not prepared)"
        return f" (~{inbox.counts[group] * (3 if group == 'review' else 1)} min)"

    return " · ".join(
        f"{name} {inbox.counts[group]}{suffix(group)}" for group, name in zip(GROUPS, names)
        if inbox.counts.get(group, 0)
    ) or "No items."


def try_lines(item: Item) -> list[str]:
    card = item.try_card
    lines = ["What changed:", card.what_changed if card else "not provided", "Try it:"]
    if card and card.kind == "web" and card.web:
        lines += [card.web.start, f"http://localhost:{card.web.port}{card.web.path}", *card.web.steps]
    else:
        lines += card.commands if card and card.commands else ["not provided"]
    lines += ["Expect:", card.expect if card else "not provided"]
    if item.prepared is None:
        lines.append("not prepared")
    else:
        lines.append(f"Prepared output (base {item.prepared.base_sha}):")
        for result in item.prepared.results:
            lines += [f"$ {result.command}", result.output]
            if result.exit_code != 0 and result.stderr:
                lines.append("stderr:\n" + result.stderr[-1500:])
            lines.append(f"exit_code={result.exit_code}" + (" (timed out)" if result.timed_out else ""))
    return lines
