"""The one reader of an order parent's own records, apart from the order text it carries.

A parent body is the order's summary and complete spec (#2161), the child checklist,
then the records publication writes: the plan marker and, for owner-label parents,
the contract line. The last plan marker is therefore the parent's own, for parents
published before and after #2161. Publication refuses order text with a line that
could read as a record (``refusals``), so the text is never rewritten.
"""

import re

PLAN = re.compile(r"<!-- foragent:plan id=([A-Za-z0-9][A-Za-z0-9_-]*) map=(\{[^\n]*?\})"
                  r"(?: acceptance=(\{[^\n]*\}))? -->")
CHECKLIST_LINE = re.compile(r"- \[[ xX]\] #\d+\b.*")
_RECORD_LINE = "<!-- foragent:"
_FENCE = re.compile(r"\s{0,3}(`{3,}|~{3,})")


def records(body: str) -> str:
    """From the parent's own (last) plan marker to the end; "" for an issue without one."""
    body = body or ""
    own = list(PLAN.finditer(body))[-1:]
    return body[own[0].start():] if own else ""


def own_plan(body: str) -> re.Match | None:
    """The parent's own plan marker: id, map and acceptance groups."""
    return PLAN.match(records(body))


def order_text(body: str) -> str:
    """The text before the parent's own records: summary, spec and checklist."""
    body = body or ""
    own = records(body)
    return body[:len(body) - len(own)] if own else body


def _lines(text: str) -> tuple[list[tuple[str, bool]], bool]:
    """Each line with whether it sits inside a code fence (fence lines count as inside), and
    whether a fence is still open at the end."""
    lines, fence = [], None
    for line in (text or "").splitlines():
        opened = _FENCE.match(line)
        if fence is not None:
            if opened and opened[1][0] == fence[0] and len(opened[1]) >= len(fence) and not line.strip(" `~"):
                fence = None
            lines.append((line, True))
        elif opened:
            fence = opened[1]
            lines.append((line, True))
        else:
            lines.append((line, False))
    return lines, fence is not None


def _record_like(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith(_RECORD_LINE) or CHECKLIST_LINE.fullmatch(stripped) is not None


def refusals(text: str) -> list[str]:
    """Lines of order text, outside code fences, that a reader could take for a record."""
    lines, open_at_end = _lines(text)
    found = [line for line, fenced in lines if not fenced and _record_like(line)]
    # An unclosed fence would swallow the generated checklist and records after the text.
    return found + (["(an unclosed code fence)"] if open_at_end else [])


def without_checklist(body: str) -> str:
    """The order text without the generated child checklist (publication refuses look-alikes outside fences)."""
    return "\n".join(line for line, fenced in _lines(order_text(body))[0]
                     if fenced or not CHECKLIST_LINE.fullmatch(line.strip()))
