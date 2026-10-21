"""Acceptance test for design-first. Locked by Foragent; do not edit."""

import pytest


def test_lint_finds_each_raw_literal_and_ignores_default_tokens_and_non_ui_files(tmp_path, monkeypatch, capsys):
    import json

    from foragent.cli import main

    monkeypatch.chdir(tmp_path)
    ui = tmp_path / "ui"
    ui.mkdir()
    clean = {"tokens.css": ":root {--color: #ffffff;}", "tokens.ts": 'export const color = "#fff";',
             "clean.svelte": "<style>a {color: var(--color)}</style>", "notes.py": 'color = "#123456"'}
    for name, content in clean.items():
        (ui / name).write_text(content)
    assert main(["design", "lint", "--json"]) == 0
    json.loads(capsys.readouterr().out)
    violations = {"short.css": "a { color: #abc; }", "long.tsx": 'const x = <div style={{color: "#123456"}}/>;',
                  "rgb.jsx": 'const x = <div style={{color: "rgb(1, 2, 3)"}}/>;',
                  "hsl.vue": "<style>a {color: hsl(10, 20%, 30%)}</style>"}
    for name, content in violations.items():
        (ui / name).write_text(content)
    assert main(["design", "lint", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    text = json.dumps(payload)
    for filename in ("short.css", "long.tsx", "rgb.jsx", "hsl.vue"):
        assert filename in text


def test_lint_custom_tokens_and_globs_and_empty_matches_exit_cleanly(tmp_path, monkeypatch, capsys):
    import json

    from foragent.cli import main

    monkeypatch.chdir(tmp_path)
    (tmp_path / "palette.css").write_text(":root {--brand: #123456;}")
    (tmp_path / "component.html").write_text('<p style="color: var(--brand)">Hello</p>')
    (tmp_path / "excluded.tsx").write_text('const color = "#bad";')
    assert main(["design", "lint", "--tokens", "palette.css", "--glob", "*.css", "--glob", "*.html", "--json"]) == 0
    json.loads(capsys.readouterr().out)
    assert main(["design", "lint", "--glob", "missing/**/*.html", "--json"]) == 0
    json.loads(capsys.readouterr().out)
    (tmp_path / "component.html").write_text('<p style="color: #bad">Hello</p>')
    assert main(["design", "lint", "--glob", "*.html"]) == 1


@pytest.mark.parametrize("frameworks,html", [(["react", "next"], False), (["vue", "nuxt"], False),
                                            (["svelte", "solid-js"], False), ([], True), ([], False)])
def test_order_context_detects_ui_and_design_foundation(tmp_path, monkeypatch, capsys, frameworks, html):
    import json

    from foragent import cli
    from foragent.tracker import FakeTracker

    (tmp_path / ".foragent.yml").write_text("repo: owner/repo\n")
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {name: "*" for name in frameworks}}))
    if html:
        (tmp_path / "index.html").write_text("<main>Hello</main>")
    monkeypatch.setattr(cli, "GhTracker", lambda repo: FakeTracker())
    assert cli.main(["order", "context", "--repo-root", str(tmp_path), "--json"]) == 0
    ui = json.loads(capsys.readouterr().out)["ui"]
    assert set(ui["frameworks"]) == set(frameworks + (["html"] if html else []))
    assert ui["has_design_foundation"] is False
    contract = tmp_path / "docs/design/CONTRACT.md"
    contract.parent.mkdir(parents=True)
    contract.write_text("# Design foundation\n")
    assert cli.main(["order", "context", "--repo-root", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ui"]["has_design_foundation"] is True


def test_order_and_init_skills_include_design_foundation_workflow():
    from pathlib import Path
    import re

    root = Path(__file__).resolve().parents[2]
    order = (root / "skills/fa-order/SKILL.md").read_text()
    init = (root / "skills/fa-init/SKILL.md").read_text()
    assert re.search(r"^#{1,6}\s+(?:\d+[.)]\s+)?Design first\b", order, re.M | re.I)
    normalized = " ".join(order.lower().split())
    for required in ("foundation", "visual", "reference apps", "this screen or the standard?", "prototype", "exempt"):
        assert required in normalized
    assert "design" in init.lower() and "lane" in init.lower()
    for skill in ("frontend-design", "design-system", "design-critique", "accessibility-review", "ux-copy", "webapp-testing"):
        assert skill in init
