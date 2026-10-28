"""Launch C4: eight local drafts; publishing remains Jin's work."""

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("platform", ["geeknews", "x", "threads", "hn"])
@pytest.mark.parametrize("language", ["ko", "en"])
def test_launch_post_is_a_nonempty_marked_local_draft(platform, language):
    path = ROOT / "docs/launch" / f"{platform}.{language}.md"
    assert path.is_file(), f"Missing {language} {platform} launch draft: {path.relative_to(ROOT)}"
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert lines and lines[0].lstrip("# ").casefold() == "draft, not posted", (
        "The first nonblank header must say 'draft, not posted'")
    assert len(lines) > 1 and any(any(c.isalnum() for c in line) for line in lines[1:]), (
        "Include draft post copy after the header")
