"""Acceptance test for facts-plan-pause-label. Locked by Foragent; do not edit.

`fa orch facts` must tell a lane held by the orchestrator's plan pause
(`fa orch pause LANE --until ... --why ...`) apart from an executor usage-limit
pause. A plan pause is not quota: reporting it as one tells the orchestrator
that executor usage ran out when it did not.
"""

from datetime import timedelta
import json

import pytest

from test_orch_facts import NOW, approved_issue, facts, facts_case, no_facts_processes  # noqa: F401
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401

WHY = "merge owner first"
PLAN_UNTIL = NOW + timedelta(days=6)
QUOTA_UNTIL = NOW + timedelta(hours=2)
PLAN_ISSUE, QUOTA_ISSUE, FREE_ISSUE = 11, 22, 33
ORCH = ("approved", "ready", "lane:orch")
DOCS = ("approved", "ready", "lane:docs")


def two_lane_case(tmp_path, monkeypatch):
    """Lane `orch` runs on claude, lane `docs` on codex; nothing runs, slots are free."""
    from foragent import capacity

    case = facts_case(tmp_path, monkeypatch)
    with (case.root / ".foragent.yml").open("a", encoding="utf-8") as config:
        config.write("executor: claude\nreviewer: null\nmax_parallel: 4\nlanes:\n"
                     "  orch:\n    paths: ['src/orch/**']\n"
                     "  docs:\n    paths: ['docs/**']\n    executor: codex\n")
    capacity.write_state(case.store, 4, NOW)
    return case


def plan_pause(case, capsys, lane):
    capsys.readouterr()
    code = cli_exit(case.main, ["orch", "pause", lane, "--until", PLAN_UNTIL.isoformat(), "--why", WHY])
    output = capsys.readouterr()
    assert code == 0, f"fa orch pause must succeed: {output.out}{output.err}"


def usage_limit_pause(case, executor):
    case.store.pause_executor(executor, QUOTA_UNTIL)


def held(result):
    return result["resources"]["parallelizable_work"]["held"]


def test_plan_paused_lane_is_reported_as_a_plan_pause_not_quota(tmp_path, monkeypatch, capsys):
    case = two_lane_case(tmp_path, monkeypatch)
    approved_issue(case, PLAN_ISSUE, ["src/orch/one.py"], labels=ORCH)
    plan_pause(case, capsys, "orch")

    result = facts(case, capsys)
    reason = held(result).get(str(PLAN_ISSUE))
    assert reason is not None, "A plan-paused lane's waiting issue is held from starting"
    assert reason != "quota", "A plan pause is not a usage-limit pause"
    assert "codex" not in result["resources"]["quota"] and not any(
        value.get("paused") for value in result["resources"]["quota"].values()), (
        "No executor is paused; a plan pause must not appear as one")

    binding = result["binding"]
    assert isinstance(binding, dict), "The plan pause holds the only waiting issue, so it binds"
    assert binding["kind"] != "quota", "The binding must not blame executor usage for a plan pause"
    assert f"#{PLAN_ISSUE}" in binding["detail"] and "orch" in binding["detail"], (
        "The binding names the held issue and the paused lane")
    assert WHY in json.dumps(result) and PLAN_UNTIL.isoformat()[:16] in json.dumps(result), (
        "Facts carry the plan pause's reason and until time")

    human = facts(case, capsys, human=True)
    binding_line = next(line for line in human.splitlines() if "Binding:" in line)
    assert "usage-limit" not in binding_line and "quota" not in binding_line, binding_line
    assert f"#{PLAN_ISSUE}" in binding_line, binding_line


def test_usage_limit_pause_is_still_reported_as_quota(tmp_path, monkeypatch, capsys):
    case = two_lane_case(tmp_path, monkeypatch)
    approved_issue(case, QUOTA_ISSUE, ["docs/guide.md"], labels=DOCS)
    usage_limit_pause(case, "codex")

    result = facts(case, capsys)
    assert held(result) == {str(QUOTA_ISSUE): "quota"}
    assert result["resources"]["quota"]["codex"]["paused"] is True
    assert result["binding"]["kind"] == "quota" and f"#{QUOTA_ISSUE}" in result["binding"]["detail"]


def test_plan_pause_and_usage_limit_pause_together_are_each_named(tmp_path, monkeypatch, capsys):
    case = two_lane_case(tmp_path, monkeypatch)
    approved_issue(case, PLAN_ISSUE, ["src/orch/one.py"], labels=ORCH)
    approved_issue(case, QUOTA_ISSUE, ["docs/guide.md"], labels=DOCS)
    plan_pause(case, capsys, "orch")
    usage_limit_pause(case, "codex")

    result = facts(case, capsys)
    reasons = held(result)
    assert set(reasons) == {str(PLAN_ISSUE), str(QUOTA_ISSUE)}, reasons
    assert reasons[str(QUOTA_ISSUE)] == "quota", "The executor usage-limit pause is quota"
    assert reasons[str(PLAN_ISSUE)] not in ("quota", reasons[str(QUOTA_ISSUE)]), (
        "The plan pause gets its own reason, distinct from the usage-limit pause")
    binding = result["binding"]
    assert isinstance(binding, dict)
    if binding["kind"] == "quota":
        assert f"#{QUOTA_ISSUE}" in binding["detail"], binding
        assert f"#{PLAN_ISSUE}" not in binding["detail"], (
            "The usage-limit binding must not claim the plan-paused issue")
    else:
        assert f"#{QUOTA_ISSUE}" not in binding["detail"] and "usage-limit" not in binding["detail"], (
            "A plan-pause binding must not claim the usage-limited issue")


def test_plan_pause_on_one_lane_leaves_other_lanes_free(tmp_path, monkeypatch, capsys):
    case = two_lane_case(tmp_path, monkeypatch)
    approved_issue(case, PLAN_ISSUE, ["src/orch/one.py"], labels=ORCH)
    approved_issue(case, FREE_ISSUE, ["docs/guide.md"], labels=DOCS)
    plan_pause(case, capsys, "orch")

    result = facts(case, capsys)
    reasons = held(result)
    assert list(reasons) == [str(PLAN_ISSUE)] and reasons[str(PLAN_ISSUE)] != "quota", reasons
