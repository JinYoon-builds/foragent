"""blocked_by entries that name an already published issue (``#N``), beside plan keys.

An order's new issues and edits may wait on an existing issue. Publication records it as
the GitHub blocker, like a plan-key dependency; a ``#N`` that does not exist is refused
before anything is written.
"""

from __future__ import annotations

import re

_EXISTING = re.compile(r"#([1-9][0-9]*)")


def existing(key: str) -> int | None:
    """The issue number ``key`` names (``#N``), or None for a plan key."""
    found = _EXISTING.fullmatch(key) if isinstance(key, str) else None
    return int(found[1]) if found else None


def resolve(mapping: dict[str, int], key: str) -> int:
    """The issue number a blocked_by entry stands for once the plan's children exist."""
    number = existing(key)
    return number if number is not None else mapping[key]


def check(plan: dict, by_number: dict, tracker) -> None:
    """Refuse an order naming ``#N`` for an issue that does not exist, before any write."""
    from foragent.order import PlanError
    from foragent.tracker import TrackerError

    named = sorted({number for item in [*plan["issues"], *plan.get("edits", [])]
                    for key in item.get("blocked_by", []) if (number := existing(key)) is not None})
    missing = []
    for number in named:
        if number in by_number:
            continue
        # The listing read may hold open issues only: a closed blocker is read directly.
        try:
            tracker.get_issue(number)
        except KeyError:
            missing.append(number)
        except TrackerError as exc:
            if not exc.is_not_found:
                raise
            missing.append(number)
    if missing:
        raise PlanError("blocked_by names issues that do not exist: "
                        + ", ".join(f"#{number}" for number in missing))
