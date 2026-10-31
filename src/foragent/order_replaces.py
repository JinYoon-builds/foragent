"""Explicit, approval-bound replacement of existing work by order children."""

from foragent.i18n import text
from foragent.tracker import TrackerError


def validate(item, label, language):
    values = item.get("replaces", [])
    if (not isinstance(values, list)
            or any(type(n) is not int or n <= 0 for n in values)
            or len(set(values)) != len(values)):
        raise ValueError(text("replaces_invalid", language, field=f"{label}.replaces"))


def plan_closures(plan, tracker, parent, mapping, language):
    """Resolve every target and conflict before any publication writes occur."""
    closures = {}
    edits = {item["number"] for item in plan.get("edits", [])}
    for item in plan["issues"]:
        for number in item.get("replaces", []):
            closures.setdefault(number, []).append(item["key"])
    for number in closures:
        conflict = None
        if number in edits:
            conflict = "replaces_edited"
        elif number in mapping.values():
            conflict = "replaces_approved"
        elif number == parent:
            conflict = "replaces_parent"
        else:
            try:
                issue = tracker.get_issue(number)
            except (TrackerError, KeyError) as exc:
                raise ValueError(text("replaces_missing", language, number=number)) from exc
            if issue.is_pull_request:
                conflict = "replaces_pr"
            elif "running" in issue.labels:
                conflict = "replaces_running"
        if conflict:
            raise ValueError(text(conflict, language, number=number))
    return closures


def preview(closures, language):
    return [text("replaces_preview", language, number=number, successors=", ".join(keys))
            for number, keys in closures.items()]


def close_replaced(plan, closures, tracker, mapping, approved_numbers, language):
    marker = f"<!-- foragent:replaced plan={plan['id']} -->"
    for number, keys in closures.items():
        if not any(mapping[key] in approved_numbers for key in keys):
            continue
        issue = tracker.get_issue(number)
        if issue.state == "closed" or any(marker in c.body for c in tracker.list_comments(number)):
            continue
        successors = ", ".join(f"#{mapping[key]}" for key in keys)
        comment = text("replaces_comment", language, successors=successors) + "\n\n" + marker
        tracker.close_issue(number, comment, reason="not_planned")
