"""Acceptance: the /fa-order skill switches its interview on the resolved engine."""

from pathlib import Path

SKILL = Path(__file__).resolve().parents[2] / "skills/fa-order/SKILL.md"


def test_interview_engine_branch():
    text = SKILL.read_text(encoding="utf-8")
    for needle in ("interview.engine", "ouroboros", "seed", "native", "AskUserQuestion"):
        assert needle in text, needle


def test_seed_feeds_restate_and_split_steps():
    text = SKILL.read_text(encoding="utf-8")
    seed = text.find("seed")
    assert seed >= 0
    assert seed < text.find("## 3. Restate and confirm")
    assert "## 4. Split into issues" in text
