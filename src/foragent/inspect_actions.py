"""GitHub answers and a deduplicated, injectable inbox notification."""

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC

from foragent.inspect_data import (
    DECISION_MARKER, RETRY_MARKER, InspectContext, Item, collect,
    comment_key, counts_line, has_marker, latest_flag, pending_signature,
)
from foragent import notify as notifications
from foragent.labels import NEEDS_HUMAN
from foragent.inspect_runtime import prepare, serve, stop
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
    stop(ctx)
    since = ctx.now().astimezone(UTC).isoformat()
    ctx.store.update_inspect(since=since)
    return since


def _push_item(item: Item) -> bool:
    # The orchestrator owns D71 judgment; legacy failure flags are evidence.
    return not item.answered and (
        item.reason in {"escalated", "orchestrator-timeout"}
        or (item.group == "review" and item.pr is not None)
    )


def _item_line(item: Item, language: str) -> str:
    label = f"#{item.number} {item.title}"
    if item.group == "review" and item.pr is not None:
        action = "병합할지 확인해요" if language == "ko" else "Decide whether to merge"
        label += f" · PR #{item.pr}: {action}"
    if item.reason:
        summary = " ".join((item.summary or "").split())
        reason = ": ".join(part for part in (item.reason, summary) if part)
        label += f" ({reason})"
    return " ".join(label.split())


def notify(ctx: InspectContext, notifier: Callable[[str, str], None] | None = None) -> str | None:
    with ctx.store.notify_lock():
        inbox = collect(ctx)
        state = ctx.store.load_inspect()
        delivered = set(state.notified_items)
        lines = []
        all_sent = True

        def send(line: str, link: str, *, summary: bool = False) -> bool:
            title = f"Foragent {ctx.repo}"
            if summary:
                title += " · 아침 요약이에요" if ctx.config.language == "ko" else " · Morning summary"
            lines.append(line)
            if notifier is not None:
                notifier(line, title)
                return True
            return notifications.send(replace(ctx.config, repo=ctx.repo), title, line, link)

        for item in inbox.items:
            if not _push_item(item) or item.notification_id is None or item.notification_id in delivered:
                continue
            if send(_item_line(item, ctx.config.language), item.url):
                delivered.add(item.notification_id)
                # Persist each success: a later delivery failure must not repeat it.
                ctx.store.update_inspect(notified_items=sorted(delivered))
            else:
                all_sent = False

        local_now = ctx.now().astimezone()
        today = local_now.date().isoformat()
        if local_now.hour == ctx.config.notify_summary_hour and state.summary_date != today:
            if send(counts_line(inbox), f"https://github.com/{ctx.repo}/issues", summary=True):
                ctx.store.update_inspect(summary_date=today)
            else:
                all_sent = False
        if all_sent:
            ctx.store.update_inspect(notified_signature=pending_signature(inbox))
        return "\n".join(lines) or None
