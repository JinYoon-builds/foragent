"""Acceptance test for tidy-cycle. Locked by Foragent; do not edit."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
import json
import re

from test_order_progress import offline_local_cli
from test_skills_autosync import cli_exit, isolated_home
from test_usage_read import usage_event


def tidy_command(case, *args):
    return cli_exit(case.main, ["tidy", *args, "--repo-root", str(case.root)])


def due(case, capsys):
    capsys.readouterr()
    assert tidy_command(case, "due", "--json") == 0
    result = json.loads(capsys.readouterr().out)
    assert {"due", "reason", "merges_since", "last_tidy_at"} <= result.keys()
    assert type(result["due"]) is bool and type(result["merges_since"]) is int
    assert result["reason"] in ("merges", "idle-quota", None)
    return result


def record_finish(case, number, *, outcome="merged"):
    case.store.start_run(number, branch=f"foragent/issue-{number}", worktree=case.store.worktree_path(number))
    case.store.finish_run(number, outcome=outcome)


def test_tidy_cycle_thirty_merges_trigger_and_done_resets_persistently(tmp_path, monkeypatch, capsys):
    from foragent.state import StateStore

    case = offline_local_cli(tmp_path, monkeypatch)
    # Non-merge outcomes must not advance the threshold.
    record_finish(case, 100, outcome="blocked")
    for number in range(1, 30):
        record_finish(case, number)
    assert due(case, capsys) == {"due": False, "reason": None, "merges_since": 29, "last_tidy_at": None}
    record_finish(case, 30)
    assert due(case, capsys) == {"due": True, "reason": "merges", "merges_since": 30, "last_tidy_at": None}
    before = datetime.now(UTC)
    assert tidy_command(case, "done") == 0
    # A new CLI invocation opens a fresh StateStore through for_repo().
    case.store = StateStore(case.store.root)
    after = due(case, capsys)
    assert after["due"] is False and after["reason"] is None and after["merges_since"] == 0
    assert before <= datetime.fromisoformat(after["last_tidy_at"]) <= datetime.now(UTC)
    record_finish(case, 31)
    assert due(case, capsys)["merges_since"] == 1
    assert case.gh_calls == []


def test_tidy_cycle_idle_quota_uses_recorded_executor_usage_and_active_lanes(git_history, tmp_path, monkeypatch, capsys):
    from foragent.executors import ExecResult
    from foragent.runner import run_issue
    from test_local_view import local_cli, record_gh
    from test_orch_review_handoff import make_case

    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true")
    calls = record_gh(monkeypatch)

    def execute(**kwargs):
        case.implement(kwargs)
        log = kwargs["log_path"]
        log.parent.mkdir(parents=True, exist_ok=True)
        # Use the existing executor-event fixture and the real runner ingestion path.
        event = usage_event("claude", used_percent=40)
        resets = int((case.clock[0] + timedelta(hours=5)).timestamp())
        event["rate_limit_info"]["resetsAt"] = resets
        for window in event["rate_limit_info"]["unifiedWindows"].values():
            window["resetsAt"] = resets
        with log.open("a") as stream:
            stream.write(json.dumps(event) + "\n")
        return ExecResult(0, False, "Implemented", log)

    monkeypatch.setattr(case.implementer, "run", execute)
    assert run_issue(1, case.ctx) == "merged"
    # local_cli's no-polling guard applies to observation, after runner polling ends.
    case.main = local_cli(case, monkeypatch)
    idle = due(case, capsys)
    assert idle["due"] is True and idle["reason"] == "idle-quota" and idle["merges_since"] == 1
    case.store.start_run(2, branch="foragent/issue-2", worktree=case.store.worktree_path(2), lane="main")
    busy = due(case, capsys)
    assert busy["due"] is False and busy["reason"] is None
    assert calls == []


def test_tidy_cycle_no_merges_and_no_usage_are_not_a_reason_to_tidy(tmp_path, monkeypatch, capsys):
    case = offline_local_cli(tmp_path, monkeypatch)
    assert due(case, capsys) == {"due": False, "reason": None, "merges_since": 0, "last_tidy_at": None}
    assert case.gh_calls == []


def test_tidy_cycle_skill_reads_widely_prioritizes_and_proposes_owned_lane_work():
    text = (Path(__file__).resolve().parents[2] / "skills/fa-orchestrator/SKILL.md").read_text().lower()
    section = re.search(r"(?ms)^#{1,4}[^\n]*(?:tidy|정리)[^\n]*\n(.*?)(?=^#{1,4} |\Z)", text)
    assert section, "The orchestrator needs an actionable tidy section (D68)"
    tidy = " ".join(section[1].split())
    assert "fa tidy due" in tidy and "fa tidy done" in tidy
    for concept in (r"(?:big|large) files", r"duplicat", r"(?:two|2|shared|overlap).{0,80}lanes?",
                    r"(?:unused|dead) code", r"superseded decisions", r"tech.debt"):
        assert re.search(concept, tidy), concept
    formula = re.sub(r"\s", "", tidy.replace("**", "").replace("`", "")).replace("×", "*").replace("−", "-")
    assert "(impact+risk)*(6-effort)" in formula
    assert re.search(r"(?:propose|proposal).{0,160}(?:order|tidy)", tidy)
    assert re.search(r"(?:human|person|user).{0,100}approv|approv.{0,100}(?:human|person|user)", tidy)
    assert re.search(r"lanes?.{0,120}(?:own|responsible)|(?:own|responsible).{0,120}lanes?", tidy)
    assert re.search(r"(?:all|every).{0,40}existing tests.{0,60}pass", tidy)
    assert re.search(r"behaviou?r.{0,50}(?:unchanged|same)|(?:unchanged|same).{0,50}behaviou?r", tidy)
