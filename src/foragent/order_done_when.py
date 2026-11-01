"""An edit keeps an issue's Done when lines unless it names each removed one with a reason.

Lines are compared as exact text and never judged. An explained removal stays readable on the
issue: the record is the body's last section, carried through every later edit.
"""

from __future__ import annotations

import re

from foragent.i18n import text
from foragent.issue import _sections, parse_body
from foragent.order_files import issue_body
from foragent import order_waiting

_MARKER = "<!-- foragent:done-when-removed -->"
_HEADING = "## Removed from Done when"
_START = f"{_MARKER}\n{_HEADING}\n"
_INTRO = "- Removed line:"
# An indented code block keeps a command's exact spacing, and no record line can open a section.
_INDENT = " " * 6
_REASON = "  Reason: "
_NEXT_MARKER = re.compile(r"^<!-- foragent:", re.MULTILINE)


def validate(removals, label: str) -> None:
    """Only the shape: a blank reason is reported with the other facts of the comparison."""
    from foragent.order import PlanError

    if not isinstance(removals, list):
        raise PlanError(f"{label} must be an array")
    lines = set()
    for index, entry in enumerate(removals):
        if not isinstance(entry, dict) or set(entry) != {"line", "reason"}:
            raise PlanError(f"{label}[{index}] must be an object with exactly line and reason")
        if not isinstance(entry["line"], str) or not isinstance(entry["reason"], str):
            raise PlanError(f"{label}[{index}].line and reason must be strings")
        if entry["line"] in lines:
            raise PlanError(f"{label}[{index}] names a line twice: {entry['line']}")
        lines.add(entry["line"])


def _span(body: str) -> tuple[int, int] | None:
    start = body.rfind(_START)
    if start == -1 or (start and body[start - 1] != "\n"):
        return None
    following = _NEXT_MARKER.search(body, start + len(_START))
    return start, following.start() if following else len(body)


def records(body: str | None) -> list[dict[str, str]]:
    """The removals recorded on this issue body, oldest first."""
    text_ = (body or "").replace("\r\n", "\n")
    span = _span(text_)
    if span is None:
        return []
    entries, lines = [], []
    for line in text_[span[0] + len(_START):span[1]].splitlines():
        if line.startswith(_INDENT):
            lines.append(line[len(_INDENT):])
        elif line.startswith(_REASON):
            entries.append({"line": "\n".join(lines), "reason": line[len(_REASON):]})
            lines = []
    return entries


def _flat(reason: str) -> str:
    # One line, so a reason cannot end the record or open a section or fence.
    return " ".join(reason.split())


def _render(entries: list[dict[str, str]]) -> str:
    parts = [_START]
    for entry in entries:
        code = "".join(f"{_INDENT}{line}\n" for line in entry["line"].split("\n"))
        parts.append(f"{_INTRO}\n\n{code}\n{_REASON}{entry['reason']}\n")
    return "".join(parts)


def with_record(body: str, existing: str | None, removals: list[dict[str, str]]) -> str:
    """``body`` plus the issue's earlier removals and these; unchanged when there are none."""
    entries = records(existing)
    for removal in removals:
        entry = {"line": removal["line"], "reason": _flat(removal["reason"])}
        if entry not in entries:
            entries.append(entry)
    return f"{body}\n{_render(entries)}" if entries else body


def without_record(body: str) -> str:
    """The body as rendered from a plan: a removal record is bookkeeping for the reader."""
    normalized = body.replace("\r\n", "\n")
    span = _span(normalized)
    return body if span is None else normalized[:span[0]] + normalized[span[1]:]


def _problems(item: dict, body: str, language: str) -> list[str]:
    number = item["number"]
    current, proposed = parse_body(body), parse_body(issue_body(item, "owner/repo", "main"))
    commands = current.done_when_commands
    checklist = [entry.text for entry in current.done_when_checklist]
    new_commands = proposed.done_when_commands
    new_checklist = [entry.text for entry in proposed.done_when_checklist]
    removed = [*(line for line in commands if line not in new_commands),
               *(line for line in checklist if line not in new_checklist)]
    declared = {entry["line"]: entry["reason"] for entry in item.get("done_when_removals", [])}
    recorded = records(body)
    problems = []
    for line, reason in declared.items():
        if not reason.strip():
            problems.append(text("order_done_when_blank_reason", language, number=number, line=line))
        elif line in removed:
            continue
        elif line in new_commands or line in new_checklist:
            problems.append(text("order_done_when_kept", language, number=number, line=line))
        elif {"line": line, "reason": _flat(reason)} not in recorded:
            # Only this same explanation may repeat, when a plan is published again after it applied.
            problems.append(text("order_done_when_unknown", language, number=number, line=line))
    for line in dict.fromkeys(removed):
        if line not in declared:
            problems.append(text("order_done_when_unexplained", language, number=number, line=line))
    return problems


def check(plan: dict, issues: dict, language: str) -> None:
    """Refuse every edit whose Done when loses a line without its own reason, or names a wrong one."""
    from foragent.order import DoneWhenRemovalError

    problems = [problem for item in plan.get("edits", [])
                for problem in _problems(item, issues[item["number"]].body, language)]
    if problems:
        raise DoneWhenRemovalError([*problems, text("order_done_when_hint", language)])


def prompt_section(item: dict) -> str:
    """What the implementer should know about removed lines; nothing when there are none."""
    removals = item.get("done_when_removals", [])
    if not removals:
        return ""
    return "\n## Removed from Done when\n" + "".join(
        f"- {entry['line']}\n  Reason: {_flat(entry['reason'])}\n" for entry in removals)


def seed(issue) -> dict:
    """One ``edits[]`` entry holding the issue's current sections, ready to change and publish."""
    parsed = parse_body(issue.body)
    waiting = order_waiting.section(issue.body)
    behaviour = _sections(issue.body.replace("\r\n", "\n")).get("Behaviour", [])
    statements = [line.strip().removeprefix("- ") for line in behaviour if line.strip()]
    return {
        "number": issue.number,
        # An issue's wait is published as part of its goal.
        "goal": parsed.goal if waiting is None else f"{parsed.goal}\n\n## Waiting\n{waiting}",
        "out_of_scope": parsed.out_of_scope,
        "files": parsed.files,
        "done_when": parsed.done_when_commands,
        "checklist": [entry.text for entry in parsed.done_when_checklist],
        # Dropping either would change the edit's route or the issue's exclusivity.
        **({"statements": statements} if statements else {}),
        **({"exclusive": parsed.exclusive} if parsed.exclusive else {}),
    }
