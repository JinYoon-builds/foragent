"""Acceptance test for tick-failure-alert. Locked by Foragent; do not edit."""

import json
from datetime import datetime, timedelta
from pathlib import Path
import socket
import traceback

import pytest
import yaml

from test_notify_blocked import notify_blocked_capture
from test_orch_review_handoff import bind_cli, make_case, tick_context
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401 (fixture)


# Chosen public contract: <state>/tick-outcome.json contains at, status
# (ok/failed), consecutive_failures, and, on failure, error_type, error_message
# (at most 500 characters), log_path. gate status exposes this as tick_outcome.
# The repository config key tick_failure_alert defaults to 3.
OUTCOME_FILE = "tick-outcome.json"


@pytest.fixture
def tick_case(git_history, tmp_path, monkeypatch, isolated_home):  # noqa: F811
    from foragent import cli, scheduler
    from foragent.config import load_config

    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[])
    # An idle repository keeps unrelated runner, stall and tidy alerts out of the test.
    case.store.finish_run(1, outcome="cancelled")
    case.tracker.close_issue(1)
    case.main = bind_cli(case, monkeypatch)
    (case.root / ".foragent.yml").write_text(yaml.safe_dump({
        "repo": "owner/repo", "skill_libraries": [], "notify": "claude-push",
    }), encoding="utf-8")
    # Exercise the real config loader, including the new threshold's YAML path.
    monkeypatch.setattr(cli, "load_config", load_config)
    monkeypatch.setenv("FORAGENT_ORCH_SPAWN", "off")
    case.pushes = notify_blocked_capture(monkeypatch)
    case.failure = None
    case.tick_calls = []
    recover = scheduler._recover_runs

    def recovery_step(ctx, report):
        case.tick_calls.append(ctx.now())
        if case.failure is not None:
            raise case.failure
        return recover(ctx, report)

    def unexpected(*args, **kwargs):
        pytest.fail("Offline tick acceptance tests must not launch workers or access the network")

    def context(**kwargs):
        case.ctx.config = kwargs["config"]
        return tick_context(case, start_runs=kwargs["start_runs"], baseline_launcher=unexpected)

    monkeypatch.setattr(cli, "TickContext", context)
    monkeypatch.setattr(scheduler, "_recover_runs", recovery_step)
    monkeypatch.setattr(cli, "spawn_runner", lambda *args: unexpected)
    monkeypatch.setattr(cli, "spawn_notify", lambda *args: None)
    monkeypatch.setattr(socket.socket, "connect", unexpected)
    monkeypatch.setattr(socket, "create_connection", unexpected)
    return case


def run_tick(case, capsys, failure=None):
    """Run the real fa entry point, accepting propagated or printed tracebacks.

    A fresh StateStore on every invocation models launchd's separate processes;
    no test writes outcomes, requests, traceback logs or alert suppression state.
    """
    from foragent.state import StateStore

    case.clock[0] += timedelta(minutes=5)
    case.store = StateStore(case.store.root, now=lambda: case.clock[0])
    case.failure = failure
    calls_before = len(case.tick_calls)
    capsys.readouterr()
    escaped_traceback = ""
    try:
        code = cli_exit(case.main, ["gate", "tick", "--no-start", "--json",
                                   "--repo-root", str(case.root)])
    except Exception as exc:
        if failure is None:
            raise
        assert type(exc) is type(failure) and str(exc) == str(failure), (
            "fa gate tick must load its config and reach the injected failure; "
            f"instead raised {type(exc).__name__}: {exc}"
        )
        code = 1
        escaped_traceback = "".join(traceback.format_exception(exc))
    output = capsys.readouterr()
    assert len(case.tick_calls) == calls_before + 1, (
        f"fa gate tick must load its config and reach scheduler recovery; CLI stderr: {output.err}"
    )
    if failure is None:
        assert code == 0, output.err
    else:
        assert code not in (None, 0), "A crashing gate tick must still exit nonzero"
        diagnostic = escaped_traceback + output.err
        assert "Traceback (most recent call last):" in diagnostic, "The CLI must preserve the traceback"
        assert f"{type(failure).__name__}: {failure}" in diagnostic


def outcome(case, status, streak):
    path = case.store.root / OUTCOME_FILE
    assert path.is_file(), f"fa gate tick must persist {OUTCOME_FILE}, even when the scheduler raises"
    assert path.stat().st_size <= 4096, "The latest tick outcome must stay small rather than embed a traceback"
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["status"] == status
    assert record["consecutive_failures"] == streak
    assert datetime.fromisoformat(record["at"]) == case.clock[0], "Record this tick's time, not an older tick"
    return record


def inbox(case, capsys):
    capsys.readouterr()
    assert cli_exit(case.main, ["orch", "inbox", "--json", "--repo-root", str(case.root)]) == 0
    return json.loads(capsys.readouterr().out)


def assert_alert_details(case, request, record, failure):
    assert request["from"] == "gate"
    assert request["status"] != "done", "A failed tick needs an open orchestrator request"
    push = json.dumps(case.pushes[-1], ensure_ascii=False)
    for text in (request["text"], push):
        for detail in (type(failure).__name__, str(failure), record["log_path"]):
            assert detail in text, f"Both the inbox request and the push need the error and log path: {detail}"


def test_tick_failure_alert_records_each_success(tick_case, capsys):
    case = tick_case
    for _ in range(2):
        run_tick(case, capsys)
        record = outcome(case, "ok", 0)
        assert not record.get("error_type") and not record.get("error_message")
    assert inbox(case, capsys) == []
    assert case.pushes == []


@pytest.mark.parametrize("error_type", [ValueError, RuntimeError], ids=["value-error", "runtime-error"])
def test_tick_failure_alert_records_exception_and_real_traceback(
    tick_case, capsys, error_type,
):
    case = tick_case
    failure = error_type("scheduler recovery failed: " + "details\n" * 300)
    run_tick(case, capsys, failure)
    record = outcome(case, "failed", 1)
    assert record["error_type"] == error_type.__name__
    assert record["error_message"].startswith("scheduler recovery failed:")
    assert 0 < len(record["error_message"]) <= 500, "Store a short summary rather than the full exception"
    log_path = Path(record["log_path"])
    if not log_path.is_absolute():
        log_path = case.store.root / log_path
    assert log_path.is_file(), "The recorded traceback path must point to an actual log"
    log = log_path.read_text(encoding="utf-8")
    assert "Traceback (most recent call last):" in log
    assert f"{error_type.__name__}: {failure}" in log, "The log must retain the full exception"
    assert "recovery_step" in log, "Keep the original scheduler traceback, not just an error summary"
    assert inbox(case, capsys) == []
    assert case.pushes == []


def test_tick_failure_alert_default_threshold_counts_changed_errors_and_alerts_once(
    tick_case, capsys,
):
    case = tick_case
    for number in (1, 2):
        run_tick(case, capsys, ValueError(f"scheduler recovery failure {number}"))
        assert inbox(case, capsys) == [], "Default threshold 3 must not alert early"
        assert case.pushes == []
    failure = ValueError("scheduler recovery failure 3")
    run_tick(case, capsys, failure)
    rows = inbox(case, capsys)
    assert len(rows) == 1, "The third consecutive failed tick must create one orchestrator inbox request"
    assert len(case.pushes) == 1, "The third consecutive failed tick must push once to the person"
    record = outcome(case, "failed", 3)
    assert_alert_details(case, rows[0], record, failure)
    request_id = rows[0]["id"]
    for streak in (4, 5, 6):
        run_tick(case, capsys, RuntimeError(f"another failure {streak}"))
        outcome(case, "failed", streak)
        assert [row["id"] for row in inbox(case, capsys)] == [request_id], "One request per failure streak"
        assert len(case.pushes) == 1, "Further failures, including different errors, must not spam pushes"


@pytest.mark.parametrize("threshold", [2, 4])
def test_tick_failure_alert_respects_repository_threshold(tick_case, capsys, threshold):
    case = tick_case
    path = case.root / ".foragent.yml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config["tick_failure_alert"] = threshold
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    for streak in range(1, threshold + 1):
        run_tick(case, capsys, ValueError(f"configured threshold failure {streak}"))
        rows = inbox(case, capsys)
        expected = int(streak == threshold)
        assert len(rows) == expected, f"tick_failure_alert: {threshold} must alert exactly at failure {threshold}"
        assert len(case.pushes) == expected
    outcome(case, "failed", threshold)


def test_tick_failure_alert_recovery_resolves_only_its_request_and_rearms(tick_case, capsys):
    case = tick_case
    assert cli_exit(case.main, ["orch", "request", "--from", "planning", "--text", "Unrelated request",
                               "--repo-root", str(case.root)]) == 0
    unrelated_id = inbox(case, capsys)[0]["id"]
    for _ in range(3):
        run_tick(case, capsys, ValueError("scheduler recovery failed"))
    alerts = [row for row in inbox(case, capsys) if row["from"] == "gate"]
    assert len(alerts) == 1, "Recovery needs the durable request created at the third failure"
    assert len(case.pushes) == 1
    alert_id = alerts[0]["id"]
    run_tick(case, capsys)
    outcome(case, "ok", 0)
    recovered_at = case.clock[0].isoformat()
    rows = {row["id"]: row for row in inbox(case, capsys)}
    assert rows[alert_id]["status"] == "done", "The first successful tick must resolve its alert request"
    assert any(recovered_at in note["note"] for note in rows[alert_id]["notes"]), (
        "The completion note must name the recovery time"
    )
    assert rows[unrelated_id]["status"] == "new"
    assert len(case.pushes) == 1, "Recovery must not send another push"
    run_tick(case, capsys)
    assert len(case.pushes) == 1
    for streak in (1, 2, 3):
        run_tick(case, capsys, ValueError("scheduler recovery failed"))
        outcome(case, "failed", streak)
        alerts = [row for row in inbox(case, capsys) if row["from"] == "gate"]
        assert len(alerts) == (2 if streak == 3 else 1), "A recovered streak must rearm the threshold"
        assert len(case.pushes) == (2 if streak == 3 else 1)
    assert next(row for row in alerts if row["id"] == alert_id)["status"] == "done"
    assert next(row for row in alerts if row["id"] != alert_id)["status"] != "done"


def test_tick_failure_alert_subthreshold_success_is_silent_and_breaks_the_streak(
    tick_case, capsys,
):
    case = tick_case
    for _ in range(2):
        run_tick(case, capsys, ValueError("short outage"))
        assert inbox(case, capsys) == [] and case.pushes == []
    run_tick(case, capsys)
    assert inbox(case, capsys) == [] and case.pushes == [], "Two failures then success must stay silent"
    outcome(case, "ok", 0)
    for streak in (1, 2):
        run_tick(case, capsys, ValueError("short outage"))
        outcome(case, "failed", streak)
        assert inbox(case, capsys) == [] and case.pushes == [], "Count consecutive failures, not lifetime failures"


def test_tick_failure_alert_status_exposes_last_outcome_and_streak(tick_case, capsys):
    case = tick_case
    for streak in (1, 2, 0):
        failure = ValueError(f"status failure {streak}") if streak else None
        run_tick(case, capsys, failure)
        assert cli_exit(case.main, ["gate", "status", "--json", "--repo-root", str(case.root)]) == 0
        status = json.loads(capsys.readouterr().out)
        assert "tick_outcome" in status, "fa gate status --json must expose the last tick outcome and failure streak"
        assert status["tick_outcome"] == outcome(case, "failed" if streak else "ok", streak)
