import json

from foragent import cli, order
from foragent.config import Config
from foragent.tracker import FakeTracker


def test_lint_reports_locations_once_with_multiple_and_repeated_options(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "palette.css").write_text(":root { --brand: #abc; }")
    (tmp_path / "palette.ts").write_text('export const brand = "#abcdef";')
    (tmp_path / "screen.css").write_text("a {\n  color: #AbC; background: RGB(1, 2, 3);\n}\n")
    (tmp_path / "screen.html").write_text('<p style="color: HSL(10, 20%, 30%)">Hello</p>')
    (tmp_path / "excluded.tsx").write_text('const color = "#123456";')

    assert cli.main(["design", "lint", "--tokens", "palette.css", "palette.ts",
                     "--glob", "*.css", "*.html", "--glob", "screen.*", "--json"]) == 1
    violations = json.loads(capsys.readouterr().out)["violations"]
    assert len(violations) == 3
    assert violations[0] == {"path": "screen.css", "line": 2, "column": 10, "literal": "#AbC"}
    assert [item["literal"] for item in violations] == ["#AbC", "RGB(", "HSL("]


def test_lint_token_paths_replace_defaults_and_plain_output_is_actionable(tmp_path, capsys):
    (tmp_path / "tokens.css").write_text(":root { --brand: #123456; }")
    (tmp_path / "palette.css").write_text(":root { --other: #abc; }")
    (tmp_path / "alias.css").symlink_to(tmp_path / "palette.css")
    assert cli.main(["design", "lint", "--repo-root", str(tmp_path),
                     "--tokens", str(tmp_path / "palette.css"), "--glob", "**/*.css"]) == 1
    output = capsys.readouterr().out
    assert "tokens.css:1:" in output and "#123456" in output
    assert "palette.css:" not in output and "alias.css:" not in output
    assert "1 raw color" in output


def test_context_discovers_ui_packages_without_dependency_or_broken_manifest_noise(tmp_path):
    for directory, package in {
        "apps/web": {"dependencies": {"react": "*", "next": "*"}},
        "apps/admin": {"devDependencies": {"vue": "*"}},
        "node_modules/ignored": {"dependencies": {"solid-js": "*"}},
        ".git/ignored": {"dependencies": {"nuxt": "*"}},
        "apps/broken": {"dependencies": [], "devDependencies": None},
    }.items():
        path = tmp_path / directory
        path.mkdir(parents=True)
        (path / "package.json").write_text(json.dumps(package))
    (tmp_path / "package.json").write_text("{")
    (tmp_path / "apps/admin/index.html").write_text("<main>Hello</main>")
    contract = tmp_path / "docs/design/CONTRACT.md"
    contract.mkdir(parents=True)  # A directory with this name is not a foundation file.
    result = order.context(tmp_path, Config(), FakeTracker())
    assert result["ui"] == {"frameworks": ["html", "next", "react", "vue"],
                            "has_design_foundation": False}
