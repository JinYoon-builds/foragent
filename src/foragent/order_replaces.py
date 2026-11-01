"""Explicit, approval-bound replacement of existing work by order children."""

from foragent.i18n import text
from foragent.tracker import TrackerError


def validate(item, label, language):
    values = item.get("replaces", [])
    if (not isinstance(values, list)
            or any(type(n) is not int or n <= 0 for n in values)
            or len(set(values)) != len(values)):
        raise ValueError(text("replaces_invalid", language, field=f"{label}.replaces"))


def _read(tracker, number):
    """The issue on GitHub now, and as unsent writes will leave it (None without any)."""
    remote = tracker.get_issue(number)
    return remote, getattr(tracker, "pending_issue", lambda _number: None)(number)


def _running(issue):
    # A closed issue no longer runs, whatever label it kept: republishing stays a no-op.
    return issue is not None and issue.state == "open" and "running" in issue.labels


def _conflict(remote, pending):
    if remote.is_pull_request:
        return "replaces_pr"
    # Neither view hides the other: unsent writes must not mask a run GitHub already shows.
    return "replaces_running" if _running(remote) or _running(pending) else None


def plan_closures(plan, tracker, parent, mapping, language):
    """Resolve every target and conflict; publication calls this again each time the facts may have moved."""
    closures = {}
    edits = {item["number"] for item in plan.get("edits", [])}
    for item in plan["issues"]:
        for number in item.get("replaces", []):
            closures.setdefault(number, []).append(item["key"])
    for number in closures:
        if number in edits:
            conflict = "replaces_edited"
        elif number in mapping.values():
            conflict = "replaces_approved"
        elif number == parent:
            conflict = "replaces_parent"
        else:
            try:
                conflict = _conflict(*_read(tracker, number))
            except KeyError as exc:
                raise ValueError(text("replaces_missing", language, number=number)) from exc
            except TrackerError as exc:
                if not exc.is_not_found:
                    raise
                raise ValueError(text("replaces_missing", language, number=number)) from exc
        if conflict:
            raise ValueError(text(conflict, language, number=number))
    return closures


def _marked(plan_id, tracker, number):
    marker = f"<!-- foragent:replaced plan={plan_id} -->"
    return any(marker in comment.body for comment in tracker.list_comments(number))


def _next_effect(marked, remote, pending):
    """Which part of this order's closure is still missing, from the facts on the issue.

    The close comes first and its marked comment records it, so a marker always means this
    order closed the issue once: an open issue with the marker was reopened by a person, and
    a closure the order made without its comment yet still gets one. Other closures stay as
    they are, including one still waiting to be sent.
    """
    if marked:
        return None
    if remote.state == "open":
        return None if pending is not None and pending.state == "closed" else "close"
    return "comment" if remote.state_reason == "not_planned" else None


def preview(plan, closures, tracker, approved_keys, language):
    """List what this publication would close, by the same rules the closure uses."""
    return [text("replaces_preview", language, number=number, successors=", ".join(keys))
            for number, keys in closures.items()
            if set(keys) & approved_keys
            and _next_effect(_marked(plan["id"], tracker, number), *_read(tracker, number)) == "close"]


def close_replaced(plan, closures, tracker, mapping, approved_numbers, language):
    """Judge every target last, after all other reads, so a conflict seen by then refuses the whole set.

    GitHub has no conditional close, so a target can still start in the instant after its
    final read. Each close therefore follows its own read: running work is never closed.
    Effects go straight to GitHub and never wait in the outbox, where a later flush could close
    a target that started meanwhile, or repeat a comment this order's next run already found.
    """
    chosen = {number: keys for number, keys in closures.items()
              if any(mapping[key] in approved_numbers for key in keys)}
    marked = {number: _marked(plan["id"], tracker, number) for number in chosen}
    views = {number: _read(tracker, number) for number in chosen}
    for number, view in views.items():
        if conflict := _conflict(*view):
            raise ValueError(text("replaces_late", language, reason=text(conflict, language, number=number)))
    direct = getattr(tracker, "unqueued", lambda: tracker)()
    closed = []
    for number, keys in chosen.items():
        effect = _next_effect(marked[number], *views[number])
        if effect is None:
            continue
        if effect == "close":
            if conflict := _conflict(*_read(tracker, number)):
                reason = text(conflict, language, number=number)
                numbers = ", ".join(f"#{done}" for done in closed)
                raise ValueError(text("replaces_late_partial", language, reason=reason, closed=numbers) if closed
                                 else text("replaces_late", language, reason=reason))
            # Not one combined call: GitHub's CLI comments before it closes, so a failed
            # close would leave the marker on an issue this order never closed.
            direct.close_issue(number, reason="not_planned")
            closed.append(number)
        successors = ", ".join(f"#{mapping[key]}" for key in keys)
        direct.comment(number, text("replaces_comment", language, successors=successors)
                       + f"\n\n<!-- foragent:replaced plan={plan['id']} -->")
