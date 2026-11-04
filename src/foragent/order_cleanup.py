"""Approval-bound board cleanup: close existing issues the person agreed to close (#1655).

A plan's ``closes`` lists them, one item each: ``{"number": N, "commit": SHA}`` closes N as
resolved by that commit, and ``{"number": N, "into": M}`` folds duplicate N into issue M.
Every target is judged before any publication write, and a close is written only when its
number is approved.
"""

import re

from foragent import order_replaces
from foragent.i18n import text
from foragent.tracker import TrackerError

_SHA = re.compile(r"[0-9a-f]{7,40}")


def validate(plan, language):
    values = plan.get("closes", [])
    if not isinstance(values, list):
        raise ValueError(text("cleanup_invalid", language, field="closes"))
    seen = set()
    for index, item in enumerate(values):
        field = f"closes[{index}]"
        if (not isinstance(item, dict) or set(item) not in ({"number", "commit"}, {"number", "into"})
                or type(item["number"]) is not int or item["number"] <= 0 or item["number"] in seen):
            raise ValueError(text("cleanup_invalid", language, field=field))
        seen.add(item["number"])
        if "commit" in item and not (isinstance(item["commit"], str) and _SHA.fullmatch(item["commit"])):
            raise ValueError(text("cleanup_commit_invalid", language, field=f"{field}.commit"))
        if "into" in item and (type(item["into"]) is not int or item["into"] <= 0
                               or item["into"] == item["number"]):
            raise ValueError(text("cleanup_into_invalid", language, field=f"{field}.into"))


def numbers(plan):
    return {item["number"] for item in plan.get("closes", [])}


def _issue(tracker, number, language):
    try:
        return tracker.get_issue(number)
    except (TrackerError, KeyError) as exc:
        raise ValueError(text("cleanup_missing", language, number=number)) from exc


def plan_cleanup(plan, tracker, parent, mapping, language):
    """Judge every listed close, approved or not, before anything is written."""
    items = list(plan.get("closes", []))
    edits = {item["number"] for item in plan.get("edits", [])}
    replaced = {n for item in plan["issues"] for n in item.get("replaces", [])}
    for item in items:
        number = item["number"]
        conflict = None
        if number in edits:
            conflict = "cleanup_edited"
        elif number in replaced:
            conflict = "cleanup_replaced"
        elif number in mapping.values():
            conflict = "cleanup_child"
        elif number == parent:
            conflict = "cleanup_parent"
        else:
            issue = _issue(tracker, number, language)
            if issue.is_pull_request:
                conflict = "cleanup_pr"
            elif "running" in issue.labels:
                conflict = "cleanup_running"
        if conflict:
            raise ValueError(text(conflict, language, number=number))
        if "into" in item and _issue(tracker, item["into"], language).is_pull_request:
            raise ValueError(text("cleanup_into_pr", language, number=number, into=item["into"]))
    return items


def rejudge(plan, tracker, parent, mapping, language):
    """Judge every close of this order, replacements and cleanups, on fresh reads.

    Publication calls this right before its first close, so a target that started running
    after the publication checks refuses all of them before any is written.
    """
    return (order_replaces.plan_closures(plan, tracker, parent, mapping, language),
            plan_cleanup(plan, tracker, parent, mapping, language))


def preview(items, language):
    return [text("cleanup_preview_commit", language, number=item["number"], commit=item["commit"])
            if "commit" in item else
            text("cleanup_preview_into", language, number=item["number"], into=item["into"])
            for item in items]


def close(plan, items, tracker, approve, selected_numbers, language):
    marker = f"<!-- foragent:cleanup plan={plan['id']} -->"
    for item in items:
        number = item["number"]
        if not (approve or number in selected_numbers):
            continue
        issue = tracker.get_issue(number)
        # Already closed, already queued, or closed once by this plan and reopened by a person.
        if (issue.state == "closed" or getattr(tracker, "close_queued", lambda _: False)(number)
                or any(marker in c.body for c in tracker.list_comments(number))):
            continue
        if "commit" in item:
            comment, reason = text("cleanup_comment_commit", language, commit=item["commit"]), "completed"
        else:
            comment, reason = text("cleanup_comment_into", language, into=item["into"]), "not_planned"
        tracker.close_issue(number, comment + "\n\n" + marker, reason=reason)
