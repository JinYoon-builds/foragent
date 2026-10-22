"""Acceptance test for design-interview. Locked by Foragent; do not edit."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]


def skill_text():
    path = ROOT / "skills/fa-design-session/SKILL.md"
    assert path.is_file(), "the design-session skill must exist"
    return " ".join(path.read_text(encoding="utf-8").casefold().split())


def test_design_interview_comes_first_and_asks_briefly():
    text = skill_text()
    assert "design interview" in text
    first = text.find("design interview")
    drafts = text.find("draft")
    assert first != -1 and drafts != -1 and first < drafts, "the interview must come before drafts"
    assert re.search(r"(?:2|two)\s*(?:–|-|to|or)\s*(?:3|three) (?:short )?questions", text)


def test_design_interview_spreads_references_wide_across_three_directions():
    text = skill_text()
    assert re.search(r"(?:about |around |~)?(?:9|nine) references", text)
    assert re.search(r"three directions", text)
    assert re.search(r"(?:neighbou?ring|adjacent|other) (?:fields|domains)", text)
    assert "axes" in text or "axis" in text
    assert "like" in text and "dislike" in text


def test_design_interview_extracts_principles_then_links_drafts_to_references():
    text = skill_text()
    assert re.search(r"(?:extract|pull out|name) (?:the )?(?:shared )?principles", text)
    assert re.search(r"draft[^.]{0,120}reference", text), "each draft names the reference it builds on"


def test_design_interview_never_commits_reference_images():
    text = skill_text()
    assert re.search(r"(?:never|do not|don't) commit[^.]{0,80}reference images", text)
    assert "link" in text and ("note" in text or "memo" in text)
    assert re.search(r"(?:do not|don't|never) copy", text)


def test_design_interview_opens_a_research_pack_for_the_medium():
    # D73 addendum: recognise the medium from the first answers and open its research pack.
    text = skill_text()
    for medium in ("web", "mobile", "terminal", "pixel", "cli"):
        assert medium in text, medium
    assert re.search(r"research pack", text)
    assert re.search(r"(?:pitfall|trap)s?", text)
    for source in ("mobbin", "lospec", "lazygit"):
        assert source in text, source
    assert "skills/fa-design-session/media/" in text and "~/.local/share/foragent/media/" in text
    assert re.search(r"(?:in the background|another agent)[^.]{0,120}(?:research|reference)|(?:research|reference)[^.]{0,120}(?:in the background|another agent)", text)


def test_design_interview_media_packs_exist_with_sources_and_pitfalls():
    media = ROOT / "skills/fa-design-session/media"
    for name in ("web", "mobile", "tui", "pixel", "cli"):
        path = media / f"{name}.md"
        assert path.is_file(), path
        body = path.read_text(encoding="utf-8").casefold()
        assert "references" in body and "pitfalls" in body, name
    tui = (media / "tui.md").read_text(encoding="utf-8")
    assert "◀" in tui and "44" in tui and "56" in tui
