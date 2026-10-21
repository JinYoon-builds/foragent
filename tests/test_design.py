import json
from pathlib import Path

import pytest

from foragent import cli, order
from foragent.config import Config
from foragent.tracker import FakeTracker


def test_lint_reports_complete_colors_and_positions_once(tmp_path, capsys):
    (tmp_path / "tokens.css").write_text(":root { --brand: #abcd; }")
    (tmp_path / "screen.css").write_text(
        "a { color: #AbC; background: #1234; }\n"
        "b { color: #AABBCC; background: #12345678; border-color: RGB(1,2,3); }\n"
        "c { color: HSL(1,2%,3%); }\n"
        'a::after { content: "#12 #12345 #1234567 #123456789 #abcdefg xrgb( #abcd-name"; }'
    )
    assert cli.main(["design", "lint", "--repo-root", str(tmp_path),
                     "--glob", "**/*.css", "screen.css", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["checked_files"] == 1
    assert report["skipped_files"] == []
    assert [item["literal"] for item in report["violations"]] == [
        "#AbC", "#1234", "#AABBCC", "#12345678", "RGB(", "HSL(",
    ]
    assert report["violations"][0] == {"path": "screen.css", "line": 1, "column": 12, "literal": "#AbC"}


@pytest.mark.parametrize("color", ["", "#169", "#8212", "#128512", "#00000065"])
def test_lint_ignores_html_character_references(tmp_path, capsys, color):
    content = "<p>&copy; &#169; &mdash; &#8212; &#128512; &#00000065; &#xA9; &#x1F600;</p>\n"
    if color:
        content += f'<p style="color: {color}">Hello</p>'
    (tmp_path / "screen.html").write_text(content)

    assert cli.main(["design", "lint", "--repo-root", str(tmp_path), "--json"]) == int(bool(color))
    report = json.loads(capsys.readouterr().out)
    assert report["checked_files"] == 1
    assert [item["literal"] for item in report["violations"]] == ([color] if color else [])


@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("has_color", [False, True])
def test_lint_replaces_invalid_utf8_and_skips_unreadable_files(
    tmp_path, monkeypatch, capsys, json_output, has_color,
):
    (tmp_path / "latin1.html").write_bytes(b"caf\xe9\n" + (b"color: #1234" if has_color else b"plain"))
    blocked = tmp_path / "blocked.css"
    blocked.write_text("a {color: #fff}")
    original = Path.read_text

    def read(path, *args, **kwargs):
        if path == blocked:
            raise PermissionError("unreadable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    args = ["design", "lint", "--repo-root", str(tmp_path)] + (["--json"] if json_output else [])
    assert cli.main(args) == int(has_color)
    captured = capsys.readouterr()
    assert captured.err == ""
    if json_output:
        report = json.loads(captured.out)
        assert report["skipped_files"] == ["blocked.css"]
        assert report["checked_files"] == 1
        assert len(report["violations"]) == int(has_color)
    else:
        assert "Skipped unreadable file: blocked.css" in captured.out
        assert f"{int(has_color)} raw color literal(s)" in captured.out


def test_lint_accepts_multiple_token_paths_and_absolute_paths(tmp_path, capsys):
    for name in ("palette.css", "theme.css", "tokens.css"):
        (tmp_path / name).write_text(":root {--color: #12345678}")
    assert cli.main(["design", "lint", "--repo-root", str(tmp_path),
                     "--tokens", str(tmp_path / "palette.css"), "theme.css",
                     "--tokens", "tokens.css", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["violations"] == []
    # Explicit token patterns replace the defaults, so tokens.css is now checked.
    assert cli.main(["design", "lint", "--repo-root", str(tmp_path),
                     "--tokens", "palette.css", "theme.css", "--json"]) == 1
    assert [item["path"] for item in json.loads(capsys.readouterr().out)["violations"]] == ["tokens.css"]


def test_context_finds_nested_ui_dependencies_but_ignores_vendor_files(tmp_path):
    frontend = tmp_path / "apps/web"
    frontend.mkdir(parents=True)
    (frontend / "package.json").write_text(json.dumps({"devDependencies": {"svelte": "*", "vite": "*"}}))
    (frontend / "index.html").write_text("<main>Hello</main>")
    vendor = tmp_path / "node_modules/demo"
    vendor.mkdir(parents=True)
    (vendor / "package.json").write_text('{"dependencies": {"react": "*"}}')
    (tmp_path / "package.json").write_bytes(b"\xff")
    assert order.context(tmp_path, Config(), FakeTracker())["ui"] == {
        "frameworks": ["html", "svelte"], "has_design_foundation": False,
    }
