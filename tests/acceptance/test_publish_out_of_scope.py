"""Acceptance test for publish-out-of-scope. Locked by Foragent; do not edit."""

import json

import pytest

from test_order import plan, publishing
from test_skills_autosync import isolated_home


def publish_cli(publication, monkeypatch):
    from foragent import cli

    root, config, tracker, git, _ = publication
    monkeypatch.setattr(cli, "load_config", lambda root: config)
    monkeypatch.setattr(cli, "GitRepo", lambda root: git)
    monkeypatch.setattr(git, "remote_slug", lambda: "owner/repo", raising=False)
    monkeypatch.setattr(cli, "GhTracker", lambda repo: tracker)
    return cli.main


@pytest.mark.parametrize("index,value", [(0, ""), (1, "   "), (0, "\n\t ")])
def test_publish_out_of_scope_empty_or_whitespace_is_rejected(
    publishing, plan, monkeypatch, capsys, index, value,
):
    main = publish_cli(publishing, monkeypatch)
    for issue in plan["issues"]:
        issue["out_of_scope"] = "Payments are outside this order."
    plan["issues"][index]["out_of_scope"] = value
    path = publishing[0] / "PLAN.json"
    path.write_text(json.dumps(plan))
    assert main(["order", "publish", str(path), "--repo-root", str(publishing[0]), "--dry-run"]) != 0
    captured = capsys.readouterr()
    assert "out_of_scope" in captured.out + captured.err
    assert publishing[2].issues == {} and publishing[-1] == []


def test_publish_out_of_scope_nonempty_values_pass_without_writes(publishing, plan, monkeypatch):
    main = publish_cli(publishing, monkeypatch)
    for issue in plan["issues"]:
        issue["out_of_scope"] = "Payments are outside this order."
    path = publishing[0] / "PLAN.json"
    path.write_text(json.dumps(plan))
    assert main(["order", "publish", str(path), "--repo-root", str(publishing[0]), "--dry-run"]) == 0
    assert publishing[2].issues == {} and publishing[-1] == []
