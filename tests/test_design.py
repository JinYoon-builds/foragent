"""Regression checks for project boundaries and resilient design lint reports."""

import json
from pathlib import Path

import pytest

from foragent.cli import main
from foragent.config import Config
from foragent.order import context
from foragent.tracker import FakeTracker


def write(root, name, source):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def test_default_scan_ignores_vendor_generated_and_hidden_directories(tmp_path, capsys):
    write(tmp_path, "src/tokens.css", ":root { --brand: #abcdef; }")
    write(tmp_path, "src/button.css", "button { color: var(--brand); }")
    for directory in (".git", ".venv", "venv", "node_modules", "dist", "build",
                      ".next", ".nuxt", "coverage", ".cache", "__pycache__", "src/node_modules"):
        write(tmp_path, f"{directory}/vendor.css", "button { color: #fff; }")
    args = ["design", "lint", "--repo-root", str(tmp_path), "--json"]
    assert main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["violations"] == []
    assert report["scanned"] == 1
    write(tmp_path, "src/button.css", "button { color: #abcd; background: #11223344; }")
    assert main(args) == 1
    report = json.loads(capsys.readouterr().out)
    assert [v["literal"] for v in report["violations"]] == ["#abcd", "#11223344"]
    assert {v["path"] for v in report["violations"]} == {"src/button.css"}


@pytest.mark.parametrize("as_json", [False, True])
def test_non_utf8_and_unreadable_files_still_print_report(tmp_path, monkeypatch, capsys, as_json):
    (tmp_path / "legacy.html").write_bytes(b'<p style="color: #abc">caf\xe9</p>')
    unreadable = write(tmp_path, "unreadable.css", "a { color: #fff; }")
    original = Path.read_text

    def read(path, *args, **kwargs):
        if path == unreadable:
            raise PermissionError("permission denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    args = ["design", "lint", "--repo-root", str(tmp_path)] + (["--json"] if as_json else [])
    assert main(args) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    if as_json:
        report = json.loads(captured.out)
        assert report["violations"] == [{"path": "legacy.html", "line": 1, "column": 18, "literal": "#abc"}]
        assert report["skipped"][0]["path"] == "unreadable.css"
    else:
        assert "legacy.html:1:18: raw color #abc" in captured.out
        assert "Skipped unreadable.css" in captured.out
    (tmp_path / "legacy.html").write_bytes(b'<p>caf\xe9</p>')
    assert main(args) == 0


def test_custom_globs_and_token_paths_are_root_relative_and_repeatable(tmp_path, capsys):
    write(tmp_path, "palette.css", ":root { --brand: #fff; }")
    write(tmp_path, "theme/colors.ts", 'export const brand = "#1234";')
    write(tmp_path, "src/button.css", "a {color: var(--brand)}")
    write(tmp_path, "src/nested/button.css", "a {color: #bad}")
    write(tmp_path, "other.css", "a {color: #bad}")
    args = ["design", "lint", "--repo-root", str(tmp_path), "--tokens", "palette.css",
            str(tmp_path / "theme/colors.ts"), "--glob", "palette.css", "theme/*.ts",
            "--glob", "src/*.css", "--json"]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["scanned"] == 1
    args[args.index("src/*.css")] = "src/**/*.css"
    assert main(args) == 1
    assert [v["path"] for v in json.loads(capsys.readouterr().out)["violations"]] == ["src/nested/button.css"]


def test_literal_lengths_and_function_case_and_locations(tmp_path, capsys):
    write(tmp_path, "colors.css", "#12 #12345 #1234567 #123456789 #abcxyz\n#ABC #ABCD #ABCDEF #ABCDEF12 RGB( HSL(")
    assert main(["design", "lint", "--repo-root", str(tmp_path), "--json"]) == 1
    violations = json.loads(capsys.readouterr().out)["violations"]
    assert [v["literal"] for v in violations] == ["#ABC", "#ABCD", "#ABCDEF", "#ABCDEF12", "RGB(", "HSL("]
    assert all(v["line"] == 2 for v in violations)


def test_ui_context_detects_workspace_frameworks_and_ignores_vendors(tmp_path):
    write(tmp_path, "package.json", '{"devDependencies": {"react": "*"}}')
    write(tmp_path, "apps/web/package.json", '{"dependencies": {"next": "*"}}')
    write(tmp_path, "apps/web/index.html", "<main>Hello</main>")
    write(tmp_path, "node_modules/vue/package.json", '{"dependencies": {"vue": "*"}}')
    write(tmp_path, "dist/package.json", '{"dependencies": {"svelte": "*"}}')
    write(tmp_path, "apps/broken/package.json", "broken")
    ui = context(tmp_path, Config(), FakeTracker())["ui"]
    assert ui == {"frameworks": ["html", "next", "react"], "has_design_foundation": False}
    write(tmp_path, "docs/design/CONTRACT.md", "# Design foundation")
    assert context(tmp_path, Config(), FakeTracker())["ui"]["has_design_foundation"] is True


def test_vendor_html_does_not_make_backend_a_ui_project(tmp_path):
    for directory in (".git", ".venv", "node_modules", "build", ".hidden"):
        write(tmp_path, f"{directory}/index.html", "<main>Dependency</main>")
    assert context(tmp_path, Config(), FakeTracker())["ui"]["frameworks"] == []
