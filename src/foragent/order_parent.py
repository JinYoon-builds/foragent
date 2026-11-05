"""Where an order parent keeps its own records, apart from the order text it carries.

The parent body carries the order's summary and complete spec verbatim (#2161), and
that text may quote any Foragent marker. Publication therefore writes the parent's own
records (plan marker, owner-label contract) as the first lines of the body, ended by a
blank line. A parent published before that keeps them after its summary, where the last
plan marker is its own.
"""

import re

from foragent.ownership import OWNED_MARKER

PLAN = re.compile(r"<!-- foragent:plan id=([A-Za-z0-9][A-Za-z0-9_-]*) map=(\{[^\n]*?\})"
                  r"(?: acceptance=(\{[^\n]*\}))? -->")
# Publication's layout: the plan marker, the owner-label contract when the parent has one, a blank line.
_HEADER = re.compile(PLAN.pattern + r"\n(?:" + re.escape(OWNED_MARKER) + r"\n)?\n")


def records(body: str) -> str:
    """The part of ``body`` that holds the parent's own records; "" for an issue with none."""
    body = body or ""
    if header := _HEADER.match(body):
        return header[0]
    own = list(PLAN.finditer(body))[-1:]
    return body[own[0].start():] if own else ""


def order_text(body: str) -> str:
    """The order text the parent carries: ``body`` without its own records."""
    body = body or ""
    if header := _HEADER.match(body):
        return body[header.end():]
    own = list(PLAN.finditer(body))[-1:]
    return body[:own[0].start()] if own else body
