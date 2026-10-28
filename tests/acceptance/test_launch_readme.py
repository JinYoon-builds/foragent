"""Launch C3: English onboarding, Korean access, and honest extension pointers."""

from pathlib import Path
import re

import pytest


ROOT = Path(__file__).resolve().parents[2]


def test_readme_is_english_first_with_korean_second():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", readme) if p.strip()]
    prose = next(p for p in paragraphs if not p.startswith(("#", ">", "```", "[", "<!--")))
    assert not re.search("[가-힣]", prose), "The first prose paragraph must onboard English readers"
    assert re.search("[A-Za-z]", prose)
    korean_file = ROOT / "README.ko.md"
    if korean_file.is_file():
        assert "README.ko.md" in readme, "Link the Korean README from the English one"
        assert re.search("[가-힣]", korean_file.read_text(encoding="utf-8"))
    else:
        heading = re.search(r"^##\s+[^\n]*(?:한국어|Korean)[^\n]*$", readme, re.M | re.I)
        assert heading, "Provide a Korean section after the English README, or link README.ko.md"
        assert re.search("[가-힣]", readme[heading.end():])


def test_readme_explains_lanes_stock_claude_self_build_and_gif_placeholder():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    lower = readme.lower()
    required = ("lanes", "runs on stock claude code", ".claude/agents", "skill.md", "project memory", "gate")
    missing = [phrase for phrase in required if phrase not in lower]
    assert not missing, f"Missing English launch explanations: {missing}"
    assert re.search(r"^##\s+[^\n]*runs on stock claude code", readme, re.M | re.I), (
        "Give stock Claude Code its own section")
    assert all(re.search(term, readme, re.I) for term in (r"\bown\w*\b", r"\blearn\w*\b", r"\bparallel\b")), (
        "Explain lanes owning areas, learning and running in parallel")
    assert re.search(r"\bgate\b[^.\n]*\boutside\b|\boutside\b[^.\n]*\bgate\b", readme, re.I), (
        "Explain that the gate runs outside Claude Code")
    assert re.search(r"foragent built itself:\s*\d+\s+merged prs\s+in\s+\d+\s+days", readme, re.I), (
        "Include a numbered self-build sentence; recount N and D from git log during implementation")
    assert any("gif" in line.lower() and re.search(r"placeholder|coming soon|todo", line, re.I)
               for line in readme.splitlines()), "Leave an explicit GIF placeholder for Jin to record"


@pytest.mark.parametrize("topic,terms,pointers", [
    ("Codex-only", (r"codex[ -]only",), ("config.resolved_reviewer", "orch_spawn")),
    ("Linux", (r"\blinux\b",), ("launchd.py",)),
    ("Windows", (r"\bwindows\b", r"not supported yet", r"\bwsl\b"), ("launchd.py",)),
    ("Orca terminals", (r"\borca\b", r"terminal"), ("watch_open.py",)),
    ("fork updates", (r"\bforks?\b", r"update"), ("update_check.py",)),
])
def test_readme_known_limits_have_contribution_pointers(topic, terms, pointers):
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    heading = re.search(r"^##\s+Known limits, contributions welcome\s*$", readme, re.M | re.I)
    assert heading, "README needs the Known limits, contributions welcome section"
    section = re.split(r"\n##\s", readme[heading.end():], maxsplit=1)[0]
    assert all(re.search(term, section, re.I) for term in terms), f"Missing known limit: {topic}"
    assert all(pointer in section for pointer in pointers), f"Missing extension pointer for {topic}: {pointers}"
