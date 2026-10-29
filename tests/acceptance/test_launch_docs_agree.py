"""Acceptance test for launch-docs-agree. Locked by Foragent; do not edit.

Docs role r26/r27 (2026-10-29): the Korean README section and docs/gate.md predate the
launch changes. They must agree with main: uv install, fa init then /fa, the starter
order, capped API-key Claude, parallel lanes, and who implements (Codex when logged in,
else Claude Code; the other tool reviews).
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _readme():
    return (ROOT / "README.md").read_text(encoding="utf-8")


def _korean(text):
    start = text.index("## 한국어")
    rest = text[start + len("## 한국어"):]
    end = rest.find("\n## ")
    return rest if end < 0 else rest[:end]


def test_korean_readme_section_matches_the_english_install_and_limits():
    korean = _korean(_readme())
    assert "uv tool install" in korean, "The Korean section carries the one-line uv install"
    assert "fa init" in korean and "/fa" in korean, "Start with fa init, then /fa"
    assert "Codex" in korean and "Claude Code" in korean
    assert "contract-proposal.md" not in korean, "Drop the superseded contract proposal link"


def test_readme_names_the_starter_order_and_the_capped_api_key_path():
    text = _readme()
    english = text[:text.index("## 한국어")]
    assert "starter" in english.lower(), "Install mentions fa init's optional starter order"
    assert "ANTHROPIC_API_KEY" in english and "spend_cap" in english, (
        "Install says API-key Claude runs only under a daily spend_cap")


def test_docs_say_who_implements_and_reviews_without_claiming_claude_always_implements():
    for path in ("README.md", "docs/launch/hn.en.md", "docs/launch/geeknews.en.md"):
        text = (ROOT / path).read_text(encoding="utf-8")
        lowered = text.lower()
        assert "codex" in lowered and "claude code" in lowered, path
        assert "review" in lowered, f"{path}: say that the other tool reviews"


def test_gate_doc_no_longer_claims_one_run_per_project():
    gate = (ROOT / "docs/gate.md").read_text(encoding="utf-8")
    assert "프로젝트당 동시 실행 1개" not in gate, "max_parallel has no default ceiling; lanes run in parallel"
