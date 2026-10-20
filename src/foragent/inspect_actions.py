"""GitHub answers and a deduplicated, injectable inbox notification."""

from collections.abc import Callable
from datetime import UTC
import subprocess

from foragent.inspect_data import (
    ACTIONABLE, DECISION_MARKER, RETRY_MARKER, InspectContext, collect,
    comment_key, counts_line, has_marker, latest_flag, pending_signature,
)
from foragent.labels import NEEDS_HUMAN
from foragent.reasons import GROUP


class InspectError(ValueError):
    """An inspect action cannot be applied to this input or GitHub state."""


def _nonempty(text: str, name: str) -> str:
    if not text.strip():
        raise InspectError(f"{name} must not be empty")
    return text.strip()


def answer(ctx: InspectContext, number: int, text: str) -> int:
    text = _nonempty(text, "decision text")
    ctx.tracker.get_issue(number)
    comments = ctx.tracker.list_comments(number)
    flag = latest_flag(comments)
    body = f"{DECISION_MARKER}\n**Decision:**\n{text}"
    decisions = [comment for comment in comments if has_marker(comment, DECISION_MARKER) and (
        flag is None or comment_key(comment) > comment_key(flag[0])
    )]
    latest = max(decisions, key=comment_key, default=None)
    return latest.id if latest is not None and latest.body == body else ctx.tracker.comment(number, body)


def retry(ctx: InspectContext, number: int) -> None:
    issue = ctx.tracker.get_issue(number)
    comments = ctx.tracker.list_comments(number)
    flag = latest_flag(comments)
    if issue.state != "open" or issue.is_pull_request or flag is None or GROUP.get(flag[1]) != "tech-failure":
        raise InspectError(f"issue #{number} is not a tech-failure item; retry refused")
    requested = any(
        has_marker(comment, RETRY_MARKER) and comment_key(comment) > comment_key(flag[0])
        for comment in comments
    )
    if NEEDS_HUMAN not in issue.labels:
        if requested:
            return
        raise InspectError(f"issue #{number} is not a tech-failure item; retry refused (no needs-human label)")
    if (pr := ctx.tracker.find_pr(f"foragent/issue-{number}")) is not None:
        ctx.tracker.close_pr(pr, "Closed by Foragent retry")
    if not requested:
        ctx.tracker.comment(number, f"{RETRY_MARKER}\nRetry requested.")
    ctx.tracker.remove_labels(number, [NEEDS_HUMAN])


def feedback(ctx: InspectContext, title: str, body: str, about: int | None = None) -> int:
    title, body = _nonempty(title, "feedback title"), _nonempty(body, "feedback body")
    if about is not None:
        body += f"\n\nAbout #{about}."
    existing = next((issue for issue in ctx.tracker.list_open_issues()
                     if "feedback" in issue.labels and issue.title == title and issue.body == body), None)
    return existing.number if existing else ctx.tracker.create_issue(title, body, ["feedback"])


def mark_seen(ctx: InspectContext) -> str:
    since = ctx.now().astimezone(UTC).isoformat()
    ctx.store.update_inspect(since=since)
    return since


def macos_notifier(line: str, title: str) -> None:
    def quoted(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    subprocess.run(
        ["osascript", "-e", f"display notification {quoted(line)} with title {quoted(title)}"],
        check=True, capture_output=True, text=True, timeout=10,
    )


def notify(ctx: InspectContext, notifier: Callable[[str, str], None] | None = None) -> str | None:
    with ctx.store.notify_lock():
        inbox = collect(ctx)
        signature = pending_signature(inbox)
        previous = ctx.store.load_inspect().notified_signature
        if signature == previous:
            return None
        if not any(item.group in ACTIONABLE for item in inbox.items):
            ctx.store.update_inspect(notified_signature=signature)
            return None
        line = counts_line(inbox)
        if ctx.config.notify is None or ctx.config.notify == "macos":
            (notifier or macos_notifier)(line, f"Foragent {ctx.repo}")
        ctx.store.update_inspect(notified_signature=signature)
        return line
