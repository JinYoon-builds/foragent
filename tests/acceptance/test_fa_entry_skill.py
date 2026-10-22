"""Acceptance test for fa-entry-skill. Locked by Foragent; do not edit."""

from pathlib import Path
import re

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]


def entry_skill_text():
    path = ROOT / "skills/fa/SKILL.md"
    assert path.is_file(), "the /fa entry skill must exist"
    return path.read_text(encoding="utf-8")


def test_fa_entry_skill_briefs_four_parts_and_confirms_profile_once_per_session():
    text = entry_skill_text()
    assert yaml.safe_load(text.split("---", 2)[1])["name"] == "fa"
    normalized = " ".join(text.casefold().split())
    assert "fa brief" in normalized
    for alternatives in (("working", "하는 일"), ("to see", "to_see", "볼 일"),
                         ("team", "팀"), ("current state", "현재 상태")):
        assert any(word in normalized for word in alternatives), alternatives
    assert "profile" in normalized
    assert re.search(r"once (?:per|each) session|once in (?:a|the) session|세션.{0,30}한 번", normalized)
    assert re.search(r"confirm|확인", normalized)


def test_fa_entry_skill_routes_unconfigured_inbox_and_normal_work_with_shared_voice():
    text = " ".join(entry_skill_text().casefold().split())
    assert re.search(r"(?:no config|unconfigured|setup.{0,12}false|설정.{0,15}없).{0,180}(?:fa-init|init flow)", text)
    assert re.search(r"(?:inbox|to_see|to see|볼 일).{0,180}(?:fa-inspect|inspect flow)", text)
    assert re.search(r"(?:else|otherwise|그 외|나머지).{0,180}(?:fa-order|order flow)", text)
    assert "voice" in text and "docs/voice.md" in text


@pytest.mark.parametrize("skill", ["fa-init", "fa-inspect", "fa-order", "fa-orchestrator"])
def test_fa_entry_skill_next_step_instructions_use_single_entry_command(skill):
    text = (ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
    instructions = re.findall(
        r"(?im)^\s*(?:[-*]|\d+\.)?\s*(?:run|type|use|실행|입력|tell the user the next step)\b[^\n]*", text)
    bad = [line for line in instructions if re.search(r"`/fa-(?:init|order|inspect)`", line, re.I)]
    assert bad == [], f"next-step instructions must name /fa: {bad}"


def test_fa_entry_skill_offers_named_interviews_at_the_end_of_the_briefing():
    # D59 addendum 2: people see "기획 인터뷰" (/fa-order) and "디자인 인터뷰" (D73); command names stay.
    text = entry_skill_text()
    for choice in ("기획 인터뷰", "디자인 인터뷰", "볼 것 처리"):
        assert choice in text, choice
    assert re.search(r"무엇을 할까요", text)
