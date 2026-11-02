"""Explicit, approval-bound replacement of existing work by order children."""

import re

from foragent.i18n import text
from foragent.tracker import TrackerError

_RECORD = re.compile(r"<!-- foragent:replacement (closing|undoing) issue=(\d+)(?: closes=(\d+))? -->")


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


def plan_closures(plan, tracker, parent, mapping, language, repair=True):
    """Resolve every target and conflict; publication calls this again each time the facts may have moved.

    A real publication refused here also undoes what an interrupted run left closed (``repair``).
    """
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
            if repair:
                _undo_interrupted(plan["id"], closures, tracker, parent, conflict, number, language)
            raise ValueError(text(conflict, language, number=number))
    return closures


def _marked(plan_id, tracker, number):
    marker = f"<!-- foragent:replaced plan={plan_id} -->"
    return any(marker in comment.body for comment in tracker.list_comments(number))


def _decisions(tracker, parent):
    """Each target's latest step and, for a decided close, how often it had been closed before."""
    if parent is None:
        return {}
    return {int(number): (step, int(closes or 0)) for comment in tracker.list_comments(parent)
            for step, number, closes in _RECORD.findall(comment.body)}


def _record(tracker, parent, step, number, language, closes=None):
    count = "" if closes is None else f" closes={closes}"
    tracker.comment(parent, text(f"replaces_{step}", language, number=number)
                    + f"\n\n<!-- foragent:replacement {step} issue={number}{count} -->")


def _next_effect(marked, decision, close_count, remote, pending):
    """Which part of this order's closure is still missing, derived from its record and the issue.

    A decided close happened if the issue has been closed more often than when it was decided.
    If so, an open issue was reopened and whoever reopened it wins; a closed one still needs its
    comment. If not, the close is still owed, whatever the issue's earlier history. A closed issue
    still being undone is also unfinished. Other closures stay as they are.
    """
    if marked:
        return None
    step, closes = decision or (None, 0)
    if remote.state == "closed":
        ours = step == "undoing" or (step == "closing" and close_count > closes)
        return "comment" if ours and remote.state_reason == "not_planned" else None
    if step == "closing" and close_count > closes:
        return None
    return "close" if pending is None or pending.state == "open" else None


def _effects(plan_id, tracker, parent, numbers):
    """Every read the decision needs except the issue itself, which the caller reads last."""
    steps = _decisions(tracker, parent)
    return {number: (_marked(plan_id, tracker, number), steps.get(number),
                     tracker.close_count(number) if number in steps else 0) for number in numbers}


def preview(plan, closures, tracker, parent, approved_keys, language):
    """List what this publication would close, by the same rules the closure uses."""
    listed = [number for number, keys in closures.items() if set(keys) & approved_keys]
    facts = _effects(plan["id"], tracker, parent, listed)
    return [text("replaces_preview", language, number=number, successors=", ".join(closures[number]))
            for number in listed if _next_effect(*facts[number], *_read(tracker, number)) == "close"]


def _started(tracker, number, unfinished):
    """The conflict of a target now; one closed but unfinished keeps the running label a late start gave it."""
    remote, pending = _read(tracker, number)
    return "replaces_running" if number in unfinished and "running" in remote.labels else _conflict(remote, pending)


def _undo(tracker, direct, parent, unfinished, conflict, number, language):
    """Refuse the whole set: every unfinished close opens again, even if one of them cannot yet.

    The record comes first, so an automatic reopen never reads as a person's. What fails stays
    unfinished, and the next publication undoes it or finishes it, whichever is then true.
    """
    failure = None
    for done in sorted(unfinished):
        try:
            _record(direct, parent, "undoing", done, language)
            direct.reopen_issue(done)
            tracker.get_issue(done)
        except TrackerError as exc:
            failure = failure or exc
    if failure is not None:
        raise failure
    raise ValueError(text("replaces_late", language, reason=text(conflict, language, number=number)))


def _undo_interrupted(plan_id, closures, tracker, parent, conflict, number, language):
    decided = [target for target in closures if target in _decisions(tracker, parent)]
    facts = _effects(plan_id, tracker, parent, decided)
    unfinished = {target for target in decided if _next_effect(*facts[target], *_read(tracker, target)) == "comment"}
    if unfinished:
        _undo(tracker, getattr(tracker, "unqueued", lambda: tracker)(), parent, unfinished, conflict, number, language)


def close_replaced(plan, closures, tracker, mapping, approved_numbers, parent, language):
    """Close every approved target or none: any listed target that starts refuses the set.

    Each run works from the facts, so it also finishes or undoes what an interrupted run left.
    GitHub has no conditional close, so each close follows its own read and a last pass over all
    targets catches a start in the instant before a close. Comments wait until that pass. Effects
    go straight to GitHub, never through the outbox, because a replayed close would override a
    person who reopened the issue meanwhile.
    """
    if not any(mapping[key] in approved_numbers for keys in closures.values() for key in keys):
        return
    facts = _effects(plan["id"], tracker, parent, closures)
    views = {number: _read(tracker, number) for number in closures}
    effects = {number: _next_effect(*facts[number], *views[number]) for number in closures}
    unfinished = {number for number, effect in effects.items() if effect == "comment"}
    direct = getattr(tracker, "unqueued", lambda: tracker)()

    def refuse_if_started(numbers):
        for number in numbers:
            if conflict := _started(tracker, number, unfinished):
                _undo(tracker, direct, parent, unfinished, conflict, number, language)

    refuse_if_started(closures)
    chosen = [number for number, keys in closures.items() if any(mapping[key] in approved_numbers for key in keys)]
    for number in chosen:
        if effects[number] == "close":
            refuse_if_started([number])
            # Not one combined call: GitHub's CLI comments before it closes.
            _record(direct, parent, "closing", number, language, tracker.close_count(number))
            direct.close_issue(number, reason="not_planned")
            unfinished.add(number)
    refuse_if_started(closures)
    for number in chosen:
        if number in unfinished:
            successors = ", ".join(f"#{mapping[key]}" for key in closures[number])
            direct.comment(number, text("replaces_comment", language, successors=successors)
                           + f"\n\n<!-- foragent:replaced plan={plan['id']} -->")
