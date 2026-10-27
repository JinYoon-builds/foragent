"""Acceptance test for fa-report-since. Locked by Foragent; do not edit."""

from datetime import UTC, datetime, timedelta
from io import BytesIO
import json
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
import yaml

from test_local_truth import record_gh
from test_skills_autosync import cli_exit, isolated_home


NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)
JUDGMENT = "Keep the API work running; the storage region needs your decision."
CURRENT = "Implement the session API"
QUESTION = "Which storage region should we use?"
RESOLVED = "The schema hold is resolved; API work can continue."


@pytest.fixture
def report_case(tmp_path, monkeypatch, isolated_home, capsys):
    from foragent import cli, orch_inbox, state, statusline
    from foragent.orch_events import append_event
    from foragent.state import StateStore
    from foragent.tracker import Issue

    root = tmp_path / "project"
    root.mkdir()
    (root / ".git").mkdir()
    values = {"repo": "offline/reports", "language": "en", "skill_libraries": [],
              "lanes": {"api": {"paths": ["api/**"]}, "decision": {"paths": ["decision/**"]}}}

    def configure(**changes):
        values.update(changes)
        (root / ".foragent.yml").write_text(yaml.safe_dump(values), encoding="utf-8")

    configure()
    clock = [NOW]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)

    for module in (cli, state, statusline):
        monkeypatch.setattr(module, "datetime", Clock)
    store = StateStore.for_repo(values["repo"], now=lambda: clock[0])
    # Use the real disk-backed store, including fresh readers on later CLI calls.
    factory = StateStore.for_repo.__func__
    monkeypatch.setattr(StateStore, "for_repo", classmethod(
        lambda cls, repo, **kwargs: factory(cls, repo, now=lambda: clock[0])))

    def merge(number, at):
        saved = clock[0]
        clock[0] = at - timedelta(minutes=10)
        store.start_run(number, branch=f"foragent/issue-{number}",
                        worktree=store.worktree_path(number), lane="api")
        clock[0] = at
        store.record_merge(number, at)
        store.finish_run(number, outcome="merged")
        clock[0] = saved

    merge(101, NOW - timedelta(hours=2))
    # Before today's midnight, but inside the first report's rolling 24 hours.
    merge(102, NOW - timedelta(hours=23, minutes=59))
    merge(103, NOW - timedelta(hours=24, minutes=1))
    issues = [Issue(301, CURRENT, "", frozenset({"running", "lane:api"}), "open"),
              Issue(201, "Schema decision", "", frozenset({"blocked", "lane:api"}), "open")]
    store.refresh_local_issues(issues, [], values["lanes"], NOW)
    store.start_run(301, branch="foragent/issue-301", worktree=store.worktree_path(301), lane="api")
    # Existing gate/orchestrator journal vocabulary: a hold followed by a retry.
    append_event(store, NOW - timedelta(hours=3), 201, "escalate", "Schema hold needs an answer")
    clock[0] = NOW - timedelta(hours=1)
    store.record_labels(201, ["blocked"], remove=True, lanes=values["lanes"])
    append_event(store, clock[0], 201, "retry", "#201: queued implementation retry", say=RESOLVED)
    append_event(store, NOW - timedelta(hours=25), 202, "retry", "Old hold resolved", say="Old schema hold resolved.")
    clock[0] = NOW
    question = orch_inbox.ask(store, QUESTION, ["Seoul", "Tokyo"], 1, issues=[401])
    # The agent supplies the judgment. Rendering facts must not invent a policy.
    append_event(store, NOW, None, "note", "Agent's current judgment", say=JUDGMENT)
    orch_inbox.write_note(store, CURRENT, "The previous hold is resolved.", JUDGMENT, {"kind": "terminal"})
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.star, "offer", lambda language: None)

    def forbidden(*args, **kwargs):
        pytest.fail("Reports must use local state only: no network, child process, or sleep")

    def fake_user(command, *args, **kwargs):
        assert command == "offline-user-statusline", f"Unexpected external command: {command!r}"
        return subprocess.CompletedProcess(command, 0, stdout=b"Claude", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_user)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(time, "sleep", forbidden)
    gh_calls = record_gh(monkeypatch)

    def brief(*options):
        capsys.readouterr()
        code = cli_exit(cli.main, ["brief", *options, "--repo-root", str(root)])
        captured = capsys.readouterr()
        assert gh_calls == [], "fa brief --report must make zero GitHub calls"
        error = captured.err.strip().splitlines()[-1] if captured.err.strip() else ""
        assert code == 0, f"fa brief {' '.join(options)} must succeed; exit={code}: {error}"
        assert not captured.err.strip(), "The report must be one stdout message, without stderr advice"
        return captured.out

    def refresh_statusline():
        record = statusline.record_path()
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps({"enabled": True, "previous_command": "offline-user-statusline"}),
                          encoding="utf-8")
        with monkeypatch.context() as streams:
            streams.delenv(statusline.NESTED_ENV, raising=False)
            streams.setattr(sys, "stdin", SimpleNamespace(buffer=BytesIO(b"{}")))
            streams.setattr(sys, "stdout", SimpleNamespace(buffer=BytesIO()))
            assert cli_exit(cli.main, ["statusline"]) == 0

    yield SimpleNamespace(root=root, store=store, clock=clock, configure=configure, merge=merge,
                          brief=brief, refresh_statusline=refresh_statusline,
                          question=question, gh_calls=gh_calls)
    assert gh_calls == [], "Reports must make zero GitHub calls, including failed runs"


def snapshot(store):
    """Observe persisted state without prescribing the report marker's filename."""
    return {path.relative_to(store.root): path.read_bytes()
            for path in store.root.rglob("*") if path.is_file()}


def contains_time(raw, expected):
    """Accept a persisted ISO time or epoch, with no new storage API required."""
    def matches(value):
        if isinstance(value, dict):
            return any(matches(item) for item in value.values())
        if isinstance(value, list):
            return any(matches(item) for item in value)
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value) == expected
            except ValueError:
                return False
        return type(value) in (int, float) and value == expected.timestamp()

    try:
        return matches(json.loads(raw))
    except (ValueError, UnicodeDecodeError):
        for line in raw.decode("utf-8").splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                value = line
            if matches(value):
                return True
        return False


@pytest.mark.parametrize("needs_person", [True, False], ids=["question-with-choices", "nothing-needs-you"])
def test_report_is_one_judgment_first_message_with_finished_now_and_persons_turn(report_case, needs_person):
    from foragent import orch_inbox
    from foragent.orch_events import append_event

    case = report_case
    judgment = JUDGMENT
    if not needs_person:
        orch_inbox.answer(case.store, case.question["id"], "Seoul")
        judgment = "Keep the API work running; nothing needs your attention."
        case.clock[0] += timedelta(seconds=1)
        append_event(case.store, case.clock[0], None, "note", "Agent's current judgment", say=judgment)
        orch_inbox.write_note(case.store, CURRENT, "No open decision.", judgment, {"kind": "terminal"})
    output = case.brief("--report")
    lines = [line for line in output.splitlines() if line.strip()]
    assert judgment in lines[0], "Lead with the agent's current judgment, before lists of facts"
    for number in (101, 102):
        assert f"#{number}" in output, f"Name finished merges by issue number: #{number}"
    assert "#103" not in output, "Do not report a merge older than the first report's 24-hour window"
    assert "#201" in output and RESOLVED in output, "Include the hold resolved in this report window"
    assert "#202" not in output, "Exclude holds resolved before the report window"
    assert sum(CURRENT in line for line in lines) == 1, "Describe what is happening now on one line"
    if needs_person:
        assert QUESTION in output, "Show the open question itself, not just its count"
        assert "Seoul" in output and "Tokyo" in output, "Show all the question's choices"
        assert any("Seoul" in line and re.search(r"recommend", line, re.I) for line in lines), (
            "Identify Seoul as the agent's recommended choice")
    else:
        assert QUESTION not in output, "Do not ask an already answered question again"
        assert re.search(r"nothing needs (?:you|your)|no (?:action|answer|decision).*needed", output, re.I), (
            "Say explicitly when nothing needs the person")


def test_report_persists_its_time_and_only_reports_new_merges_after_a_fresh_read(report_case):
    case = report_case
    before = snapshot(case.store)
    first = case.brief("--report")
    assert "#101" in first and "#102" in first, "The first report must include recent merges"
    after = snapshot(case.store)
    changed = [path for path in after if before.get(path) != after[path]]
    assert changed, "The report command must persist its report time in local state"
    assert any(contains_time(after[path], NOW) for path in changed), "Record the report's time, not a merge's time"
    case.merge(104, NOW + timedelta(minutes=5))
    case.clock[0] = NOW + timedelta(minutes=10)
    second = case.brief("--report")
    assert "#104" in second, "The next report must include the new merge"
    for number in (101, 102, 103, 201, 202):
        assert f"#{number}" not in second, f"Do not repeat old finished work: #{number}"
    latest = snapshot(case.store)
    assert any(contains_time(latest[path], case.clock[0]) for path in latest
               if latest[path] != after.get(path) and path in changed), "Advance the persisted report time"


def test_only_the_report_command_writes_or_advances_the_report_time(report_case):
    case = report_case
    before = snapshot(case.store)
    case.brief()
    case.brief("--json")
    case.refresh_statusline()
    assert snapshot(case.store) == before, "Normal brief and statusline must not create a report marker"
    case.brief("--report")
    reported = snapshot(case.store)
    assert reported != before, "Only fa brief --report must record the report time"
    case.clock[0] += timedelta(hours=1)
    case.brief()
    case.brief("--json")
    case.refresh_statusline()
    assert snapshot(case.store) == reported, "Reading brief or refreshing statusline must not advance the report time"


def test_first_report_covers_a_rolling_twenty_four_hours_not_just_today(report_case):
    case = report_case
    output = case.brief("--report")
    assert "#102" in output, "The first report includes a merge 23h59m ago, even before midnight"
    assert "#101" in output, "Include today's merge as well"
    assert "#103" not in output, "The first report excludes a merge 24h01m ago"
    assert "#201" in output and "#202" not in output, "Apply the same window to resolved holds"


@pytest.mark.parametrize("previous_report", [False, True], ids=["never-reported", "already-reported"])
def test_report_off_switch_prints_existing_brief_and_records_nothing(report_case, previous_report):
    case = report_case
    if previous_report:
        case.brief("--report")
        case.clock[0] += timedelta(hours=1)
    case.configure(presence={"report": False})
    before = snapshot(case.store)
    output = case.brief("--report")
    assert snapshot(case.store) == before, "presence.report: false must neither create nor advance report state"
    ordinary = case.brief()
    assert output == ordinary, "presence.report: false must print the existing fa brief output exactly"
    assert snapshot(case.store) == before, "The fallback brief must remain read-only"


def test_fa_skill_runs_and_shows_the_report():
    skill = (Path(__file__).resolve().parents[2] / "skills/fa/SKILL.md").read_text(encoding="utf-8")
    assert "fa brief --report" in skill, "/fa must run fa brief --report"
    assert re.search(r"(?:show|display|present|relay)[^\n.]{0,140}(?:report|output)|"
                     r"(?:report|output)[^\n.]{0,140}(?:show|display|present|relay)|"
                     r"(?:show|display|present|relay)\s+it\b", skill, re.I), (
        "/fa must tell the agent to show the report output to the person")


def test_fa_skill_shows_the_big_squirrel_and_greeting_on_first_run_in_an_empty_folder():
    skill = (Path(__file__).resolve().parents[2] / "skills/fa/SKILL.md").read_text(encoding="utf-8")
    paragraphs = re.split(r"\n\s*\n", skill)
    assert any(re.search(r"first (?:run|time)|empty (?:folder|directory)|setup.{0,20}false", part, re.I)
               and re.search(r"(?:big|large) squirrel|큰 다람쥐", part, re.I)
               and re.search(r"greet|welcome|hello|안녕|인사", part, re.I)
               and re.search(r"show|display|present|draw|보여|표시", part, re.I)
               for part in paragraphs), "/fa must show the big squirrel and greeting on first run in an empty folder"


def test_report_reads_only_local_state_and_makes_zero_github_calls(report_case):
    output = report_case.brief("--report")
    assert output.strip(), "The offline report must still produce its complete message"
    assert report_case.gh_calls == [], "Report generation must make zero GitHub calls"
