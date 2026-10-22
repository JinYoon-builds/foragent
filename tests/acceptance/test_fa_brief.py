"""Acceptance test for fa-brief. Locked by Foragent; do not edit."""

from dataclasses import replace
import json
import re

import pytest
import yaml

from test_skills_autosync import isolated_home


def briefing_case(git_history, tmp_path, monkeypatch, *, language="ko"):
    from foragent.scheduler import tick
    from test_orch_review_handoff import tick_context
    from test_view_status import status_case

    case, main = status_case(git_history, tmp_path, monkeypatch, language=language)
    lanes = {name: dict(settings) for name, settings in case.ctx.config.lanes.items()}
    lanes["api"]["skills"] = ["house-style"]
    case.ctx.config = replace(case.ctx.config, lanes=lanes, skill_libraries=[])
    case.tracker.add_issue(5, "Review this PR", labels=["approved", "lane:docs"])
    case.tracker.create_pr(head="foragent/issue-5", base="main", title="Review", body="Closes #5")
    case.tracker.add_issue(6, "Pick a policy", labels=["needs-human", "lane:decision"])
    case.tracker.comment(6, "<!-- foragent:needs-human reason=spec-question -->\nPick policy\nA or B?")
    # Import the complete pre-existing GitHub state before briefing reads it.
    tick(tick_context(case, start_runs=False))
    # Install the future run field after the import, so today's gate does not
    # deserialize an unsupported schema before the briefing under test runs.
    path = case.store.root / "runs/1.json"
    record = json.loads(path.read_text())
    record["skills_used"] = ["tone", "polish-x"]
    path.write_text(json.dumps(record))
    return case, main


def test_fa_brief_json_includes_work_queue_inbox_team_profile_and_usage(
    git_history, tmp_path, monkeypatch, capsys,
):
    case, main = briefing_case(git_history, tmp_path, monkeypatch)
    assert main(["brief", "--json", "--repo-root", str(case.root)]) == 0
    brief = json.loads(capsys.readouterr().out)
    assert {"working", "queued", "to_see", "team", "profile", "usage"} <= brief.keys()
    working, = [item for item in brief["working"] if item["issue"] == 1]
    assert {"lane", "issue", "title", "phase", "minutes"} <= working.keys()
    assert working["lane"] == "api" and working["title"] == "Implement feature"
    assert working["phase"] == "reviewing" and working["minutes"] == 2
    assert 4 in {item["issue"] for item in brief["queued"]}
    assert {3, 5, 6} <= {item["issue"] for item in brief["to_see"]}
    team = {item["lane"]: item for item in brief["team"]}
    assert {"lane", "title", "executor", "fixed_skills", "used_skills", "warnings"} <= team["api"].keys()
    assert team["api"]["fixed_skills"] == ["house-style"]
    assert team["api"]["used_skills"] == ["tone", "polish-x"]
    assert team["api"]["executor"] == case.ctx.config.executor
    assert brief["profile"] is None
    assert brief["usage"] == {}


def test_fa_brief_team_warns_when_lane_has_no_fixed_or_used_skills(git_history, tmp_path, monkeypatch, capsys):
    case, main = briefing_case(git_history, tmp_path, monkeypatch)
    assert main(["brief", "--json", "--repo-root", str(case.root)]) == 0
    team = {item["lane"]: item for item in json.loads(capsys.readouterr().out)["team"]}
    assert team["web"]["fixed_skills"] == [] and team["web"]["used_skills"] == []
    assert "no-skills" in team["web"]["warnings"]
    assert "no-skills" not in team["api"]["warnings"]


def test_fa_brief_includes_persisted_profile_and_recorded_executor_usage(git_history, tmp_path, monkeypatch, capsys):
    from foragent import cli
    from foragent.config import load_config
    from test_orch_review_handoff import bind_cli
    from test_usage_read import usage_case

    case = usage_case(git_history, tmp_path, monkeypatch)
    main = bind_cli(case, monkeypatch)
    profile = {"kind": "daily-app", "stage": "build", "priority": "speed", "confirmed_at": "2026-10-22"}
    (case.root / ".foragent.yml").write_text(yaml.safe_dump({"repo": "owner/repo", "profile": profile}))
    monkeypatch.setattr(cli, "load_config", load_config)
    capsys.readouterr()
    assert main(["brief", "--json", "--repo-root", str(case.root)]) == 0
    brief = json.loads(capsys.readouterr().out)
    assert brief["profile"] == profile
    assert brief["usage"] == case.store.usage()
    assert set(brief["usage"]) == {"claude", "codex"}


def test_fa_brief_unconfigured_repository_returns_setup_false(tmp_path, capsys):
    from foragent.cli import main

    assert main(["brief", "--json", "--repo-root", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["setup"] is False


@pytest.mark.parametrize("language", ["ko", "en"])
def test_fa_brief_plain_text_uses_config_language(git_history, tmp_path, monkeypatch, capsys, language):
    case, main = briefing_case(git_history, tmp_path, monkeypatch, language=language)
    assert main(["brief", "--repo-root", str(case.root)]) == 0
    output = capsys.readouterr().out
    assert "#1" in output and "Implement feature" in output
    assert not output.lstrip().startswith("{")
    if language == "ko":
        assert re.search("[가-힣]", output)
    else:
        assert "working" in output.casefold()
