"""Acceptance test for publish-board-duplicate. Locked by Foragent; do not edit."""

from dataclasses import replace
import json
import subprocess

from test_order import plan, publishing
from test_publish_out_of_scope import publish_cli
from test_skills_autosync import isolated_home


def board_publication(publication, plan, monkeypatch, error):
    from foragent import board
    from foragent.tracker import GhTracker

    root, config, tracker, git, events = publication
    project = board.ensure(tracker, "owner/repo", ["api"])
    assert project
    config = replace(config, board=project)
    graphql = tracker.graphql
    calls = []

    def gh_run(args, input=None):
        assert args == ["api", "graphql", "--input", "-"]
        payload = json.loads(input)
        query, variables = payload["query"], payload["variables"]
        if "addProjectV2ItemById" in query:
            calls.append(variables)
            if error == "Content already exists in this project":
                # The server has the card already, as can happen after a partial publish.
                graphql(query, variables)
            raise subprocess.CalledProcessError(1, ["gh", *args], stderr=f"gh: {error}")
        return json.dumps(graphql(query, variables))

    transport = GhTracker("owner/repo", run=gh_run)
    monkeypatch.setattr(tracker, "graphql", transport.graphql)
    for issue in plan["issues"]:
        issue["out_of_scope"] = "Other work."
    path = root / "PLAN.json"
    path.write_text(json.dumps(plan))
    return publish_cli((root, config, tracker, git, events), monkeypatch), path, calls


def test_publish_board_duplicate_existing_content_is_silent_success(publishing, plan, monkeypatch, capsys, caplog):
    error = "Content already exists in this project"
    main, path, calls = board_publication(publishing, plan, monkeypatch, error)
    assert main(["order", "publish", str(path), "--repo-root", str(publishing[0])]) == 0
    assert calls, "publish must actually try adding board content"
    captured = capsys.readouterr()
    assert error not in captured.err
    assert "Traceback" not in captured.err + caplog.text
    assert error not in caplog.text
    assert any(issue.title == "Login API" for issue in publishing[2].list_issues())


def test_publish_board_duplicate_other_gh_errors_still_fail(publishing, plan, monkeypatch, capsys):
    main, path, calls = board_publication(publishing, plan, monkeypatch, "Resource not accessible (HTTP 403)")
    assert main(["order", "publish", str(path), "--repo-root", str(publishing[0])]) != 0
    assert calls
    assert "403" in capsys.readouterr().err
