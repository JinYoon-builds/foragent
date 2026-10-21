import json

from foragent import cli, design, order
from foragent.config import Config
from foragent.tracker import FakeTracker


def test_lint_reports_source_positions_once_for_overlapping_globs(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "view.css").write_text("/* view */\na { color: #AbC; background: RGB(1, 2, 3); }\n")
    assert cli.main(["design", "lint", "--glob", "*.css", "**/*.css", "--json"]) == 1
    assert json.loads(capsys.readouterr().out) == {"violations": [
        {"file": "view.css", "line": 2, "column": 12, "literal": "#AbC"},
        {"file": "view.css", "line": 2, "column": 30, "literal": "RGB("},
    ]}


def test_multiple_token_paths_and_replacement_of_defaults(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tokens.css").write_text(":root { --default: #123456; }")
    (tmp_path / "palette.css").write_text(":root { --brand: #abc; }")
    (tmp_path / "theme.css").write_text(":root { --theme: hsl(0, 0%, 0%); }")
    assert cli.main(["design", "lint", "--tokens", str(tmp_path / "palette.css"),
                     "theme.css", "--json"]) == 1
    assert [item["file"] for item in json.loads(capsys.readouterr().out)["violations"]] == ["tokens.css"]
    assert cli.main(["design", "lint", "--tokens", str(tmp_path / "*.css")]) == 0
    assert "0 raw color" in capsys.readouterr().out


def test_hex_matches_whole_literals(tmp_path):
    (tmp_path / "view.css").write_text(
        'a { content: "#abcd #12345 #12345678 #abcdefg"; color: #ABCDEF; background: #123; }'
    )
    assert [item["literal"] for item in design.lint(tmp_path)["violations"]] == ["#ABCDEF", "#123"]


def test_context_detects_workspace_dev_dependencies_but_ignores_installed_packages(tmp_path):
    app = tmp_path / "packages/web"
    app.mkdir(parents=True)
    (app / "package.json").write_text(json.dumps({"devDependencies": {"svelte": "*", "react": "*"}}))
    (app / "index.html").write_text("<main>Welcome</main>")
    ignored = tmp_path / "node_modules/example"
    ignored.mkdir(parents=True)
    (ignored / "package.json").write_text('{"dependencies": {"vue": "*"}}')
    (tmp_path / "package.json").write_text("[]")
    ui = order.context(tmp_path, Config(), FakeTracker())["ui"]
    assert ui == {"frameworks": ["html", "react", "svelte"], "has_design_foundation": False}


def test_malformed_manifest_is_not_ui(tmp_path):
    path = tmp_path / "package.json"
    for content in ("{broken", "null", '{"dependencies": [], "devDependencies": "react"}'):
        path.write_text(content)
        assert design.package_frameworks(path) == set()
