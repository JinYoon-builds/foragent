"""Acceptance test for view-ux-copy. Locked by Foragent; do not edit."""

import re

from test_vendored_prove_it_works import ROOT, assert_lane_skills, frontmatter, normalized


def assert_kwp_apache_attribution(text, owner):
    text = normalized(text)
    assert "knowledge-work-plugins" in text, f"{owner}: credit knowledge-work-plugins"
    assert re.search(r"\bapache(?: license)?[-,\s]+(?:version )?2\.0\b", text), (
        f"{owner}: retain the Apache-2.0 attribution"
    )


def test_ux_copy_is_attributed_adapted_and_uses_existing_context():
    skill = ROOT / "skills/fa-ux-copy/SKILL.md"
    metadata, body = frontmatter(skill)
    assert metadata.get("name") == "fa-ux-copy", "Name the product skill fa-ux-copy"
    text = normalized(body)
    assert_kwp_apache_attribution(body, "fa-ux-copy")
    assert any("knowledge-work-plugins" in normalized(paragraph)
               and re.search(r"(?<!fa-)\bux-copy\b", normalized(paragraph))
               for paragraph in re.split(r"\n\s*\n", body)), "Credit knowledge-work-plugins' upstream ux-copy skill"
    assert "toss-ux-writing" in text, "Retain attribution to the merged toss-ux-writing rules"
    changes = [normalized(paragraph) for paragraph in re.split(r"\n\s*\n", body)
               if re.search(r"\b(?:changes?|changed|modifi\w*|adapt\w*)\b", normalized(paragraph))]
    assert any("docs/voice.md" in paragraph or "issue goal" in paragraph
               or ("merg" in paragraph and "toss" in paragraph) for paragraph in changes), (
        "State that ux-copy was changed and explain the context/voice/Toss adaptation"
    )
    assert re.search(r"\b(?:read|use|consult)\w*\b[^.]*\bissue(?:'s)? goal\b", text), (
        "Read the issue Goal for the copy requirements"
    )
    assert re.search(r"\b(?:read|use|consult|follow)\w*\b.*docs/voice\.md", text), (
        "Read docs/voice.md for the project's voice"
    )
    # An explicit 'do not ask the user' is allowed; positive interview instructions are not.
    instructions = re.sub(
        r"\b(?:do not|don't|never|instead of|without|rather than) "
        r"(?:ask|asking|prompt|prompting|output|return|present|provide|generate|use|using)\b[^.!?;,]*",
        "", text,
    )
    assert not re.search(
        r"\b(?:ask|asking|prompt|prompting|interview) (?:the )?user\b"
        r"|\bask (?:a |one |clarifying )*questions?\b"
        r"|\bask (?:what|which|who|how|whether)\b", instructions,
    ), "Use the issue Goal and docs/voice.md instead of instructing an agent to question the user"
    assert not re.search(
        r"\b(?:three|3)[- ]+(?:alternatives?|options?|variants?)\b[^.]*\btable\b"
        r"|\btable\b[^.]*\b(?:three|3)[- ]+(?:alternatives?|options?|variants?)\b", instructions,
    ), "Remove the three-alternatives table output requirement"
    for line in body.splitlines():
        if "|" not in line:
            continue
        cells = [normalized(cell) for cell in line.strip().strip("|").split("|")]
        alternatives = [cell for cell in cells
                        if re.match(r"(?:option|alternative|variant)[ -]*[a-c1-3]\b", cell)]
        assert len(alternatives) < 3, "Remove the three-alternatives output table template"
    tables = re.findall(r"(?:^[ \t]*\|[^\n]+\|[ \t]*(?:\n|$)){2,}", body, re.M)
    for table in tables:
        rows = [[normalized(cell) for cell in line.strip().strip("|").split("|")]
                for line in table.splitlines()]
        headers = rows[0]
        if any(cell in {"option", "alternative", "variant"} for cell in headers):
            data = [row for row in rows[1:] if not all(re.fullmatch(r":?-+:?", cell) for cell in row)]
            assert len(data) != 3, "Remove the output table containing three alternative rows"
    raw = ROOT / ".claude/skills/ux-copy"
    assert not raw.exists() and not raw.is_symlink(), "Remove the raw .claude/skills/ux-copy copy"


def test_view_lane_and_agent_load_product_ux_copy():
    assert_lane_skills("fa-ux-copy", ("view",))
