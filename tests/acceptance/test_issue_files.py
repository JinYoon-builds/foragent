"""Acceptance test for issue-files. Locked by Foragent; do not edit."""

import json
from pathlib import Path

import pytest

from test_order import plan, publishing
from test_skills_autosync import isolated_home


@pytest.mark.parametrize("files", [None, [], ["src/foragent/runner.py", "skills/**"]],
                         ids=["omitted", "empty", "paths-and-globs"])
def test_issue_files_publish_dry_run_accepts_optional_paths(publishing, plan, monkeypatch, files):
    from test_publish_out_of_scope import publish_cli

    if files is not None:
        plan["issues"][0]["files"] = files
    root, _, tracker, _, events = publishing
    path = root / "PLAN.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    main = publish_cli(publishing, monkeypatch)
    assert main(["order", "publish", str(path), "--repo-root", str(root), "--dry-run"]) == 0
    assert tracker.issues == {} and events == []


@pytest.mark.parametrize("files", [
    None, "src/foragent/runner.py", {}, 1, [""], [" \t"], [1],
    ["/src/foragent/runner.py"], [".."], ["../runner.py"],
    ["src/../runner.py"], ["skills/**/../runner.py"],
    ["src/foragent/runner.py", "/outside.py"],
])
def test_issue_files_publish_rejects_invalid_files_with_named_error(
    publishing, plan, monkeypatch, capsys, files,
):
    from test_publish_out_of_scope import publish_cli
    from test_skills_autosync import cli_exit

    plan["issues"][0]["files"] = files
    root, _, tracker, _, events = publishing
    path = root / "PLAN.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    main = publish_cli(publishing, monkeypatch)
    assert cli_exit(main, ["order", "publish", str(path), "--repo-root", str(root), "--dry-run"]) != 0
    output = capsys.readouterr()
    assert "files" in output.out + output.err
    assert tracker.issues == {} and events == []


def test_issue_files_public_body_builder_and_parser_preserve_paths_and_globs(plan):
    from foragent.issue import parse_body
    from foragent.order import issue_body

    item = plan["issues"][0]
    item["files"] = ["src/foragent/runner.py", "skills/**"]
    body = issue_body(item, "owner/repo", "main")
    assert all(path in body for path in item["files"]), body
    parsed = parse_body(body)
    assert parsed.files == item["files"]
    assert parsed.done_when_commands == item["done_when"]
    assert parsed.goal == item["goal"] and parsed.out_of_scope == item["out_of_scope"]


def test_issue_files_legacy_body_without_section_has_no_known_files():
    from foragent.issue import parse_body

    body = (
        "<!-- foragent:v1 -->\n## Goal\nImplement the feature.\n"
        "## Out of scope\nOther features.\n## Done when\n```sh\ntrue\n```\n"
    )
    parsed = parse_body(body)
    assert parsed.files == []
    assert parsed.done_when_commands == ["true"]


def test_issue_files_order_skill_requires_files_during_issue_split():
    skill = (Path(__file__).resolve().parents[2] / "skills/fa-order/SKILL.md").read_text(encoding="utf-8")
    split = skill.split("## 4. Split into issues", 1)[1].split("\n## ", 1)[0]
    assert "`files`" in split or '"files"' in split, (
        "The split instructions must name the per-issue files field, beyond general mentions of files."
    )
