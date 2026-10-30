"""Acceptance test for fa-order-design-references. Locked by Foragent; do not edit."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]


def skill(name):
    return " ".join((ROOT / f"skills/{name}/SKILL.md").read_text(encoding="utf-8").casefold().split())


def test_fa_order_design_first_carries_approved_artifacts_and_visual_issue_links():
    source = (ROOT / "skills/fa-order/SKILL.md").read_text(encoding="utf-8")
    section = re.search(r"^## Design first[^\n]*\n(.*?)(?=^## |\Z)", source, re.M | re.S)
    assert section
    text = " ".join(section[1].casefold().split())
    assert re.search(r"approved.{0,40}drafts.{0,40}prototypes.{0,40}screenshots"
                     r".{0,100}reference_files.{0,80}docs/design/<topic>/", text)
    assert re.search(r"visual issues.{0,60}(?:point|link).{0,80}(?:references|reference files)", text)


def test_design_session_continues_fa_order_here_with_handoff_and_drafts():
    text = skill("fa-design-session")
    assert re.search(r"continue.{0,50}fa-order.{0,70}same planning session", text)
    assert re.search(r"handoff\.md.{0,50}drafts.{0,100}reference_files.{0,80}docs/design/<topic>/", text)
    assert "fa orch request --from planning" not in text
    assert re.search(r"do not relay.{0,70}orchestrator.{0,40}fa orch request", text)
    assert not re.search(r"(?:pass|send).{0,30}handoff.{0,30}orchestrator", text)


def test_design_session_keeps_research_images_local_but_publishes_approved_screenshots():
    text = skill("fa-design-session")
    assert re.search(r"third-party.{0,50}reference images.{0,70}local state", text)
    assert re.search(r"approved.{0,50}screenshots.{0,100}reference_files", text)
