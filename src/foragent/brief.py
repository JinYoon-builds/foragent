"""Start-of-session briefing for the planning session (`fa brief`).

Reads local state only. #113 adds work, team and usage facts to the same
payload; this module currently reports the open human questions (D76).
"""

from __future__ import annotations

from typing import Any

from foragent import orch_inbox
from foragent.state import StateStore

_LINES = {
    "en": {"questions": "Questions to answer: {count}", "empty": "Nothing to brief."},
    "ko": {"questions": "답할 질문 {count}개", "empty": "알릴 일이 없어요."},
}


def collect(store: StateStore) -> dict[str, Any]:
    return {"questions_open": len(orch_inbox.questions(store))}


def render(brief: dict[str, Any], language: str = "en") -> str:
    words = _LINES.get(language, _LINES["en"])
    lines = []
    if brief["questions_open"]:
        lines.append(words["questions"].format(count=brief["questions_open"]))
    return "\n".join(lines) or words["empty"]
