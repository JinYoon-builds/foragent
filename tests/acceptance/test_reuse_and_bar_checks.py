"""Acceptance test for reuse-and-bar-checks. Locked by Foragent; do not edit."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def norm(text):
    return " ".join(text.lower().split())


def test_skeptic_seat_asks_for_reuse_bar_and_reversibility():
    from foragent.order_perspectives import SEAT_FOCUS

    focus = norm(SEAT_FOCUS["skeptic"])
    for phrase in ("existing code", "measured gap", "stricter", "proven", "incident",
                   "can it be undone", "detection and recovery"):
        assert phrase in focus, phrase


def test_other_users_seat_questions_maintainer_specific_assumptions():
    from foragent.order_perspectives import DEFAULT_EXECUTORS, SEAT_FOCUS, SEATS

    assert "other_users" in SEATS and "other_users" in DEFAULT_EXECUTORS
    focus = norm(SEAT_FOCUS["other_users"])
    for phrase in ("maintainer", "claude-only", "codex-only", "api", "non-korean",
                   "without a screen", "setting", "init"):
        assert phrase in focus, phrase


def test_architect_and_order_skills_carry_the_same_checks():
    for path in ("skills/fa-architect/SKILL.md", "skills/fa-order/SKILL.md"):
        text = norm((ROOT / path).read_text(encoding="utf-8"))
        for phrase in ("existing code", "measured gap", "stricter", "can it be undone",
                       "detection and recovery"):
            assert phrase in text, (path, phrase)
