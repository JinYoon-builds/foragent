"""Acceptance test for run-failure-reasons. Locked by Foragent; do not edit."""

from dataclasses import replace
import json
import os

from test_orch_review_handoff import make_case, tick_context


def history_row(case, number):
    path = case.store.root / "runs.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [row for row in rows if row["number"] == number][-1]


def test_run_failure_reasons_error_outcome_keeps_the_exception_in_the_run_record(
    git_history, tmp_path, monkeypatch,
):
    from foragent.runner import run_issue
    from foragent.tracker import TrackerError

    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true")

    def create_pr(*args, **kwargs):
        raise TrackerError("gh: HTTP 502 Bad Gateway from pulls endpoint")

    monkeypatch.setattr(case.tracker, "create_pr", create_pr)
    assert run_issue(1, case.ctx) == "error"
    run = case.store.get_run(1)
    assert run.outcome == "error"
    assert "502 Bad Gateway" in (run.detail or ""), "An error run must say why it failed"
    assert "502 Bad Gateway" in (history_row(case, 1).get("detail") or "")
    assert "\n" not in run.detail and len(run.detail) <= 500, "Keep the reason one short line"


def test_run_failure_reasons_github_failure_while_starting_is_not_a_crash(
    git_history, tmp_path, monkeypatch,
):
    from foragent.scheduler import tick
    from foragent.tracker import TrackerError

    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true")
    # make_case leaves #1 mid-run; reset it to a freshly approved issue.
    case.store.finish_run(1, outcome="not-started")
    case.tracker.remove_labels(1, ["running", "approved"])
    case.tracker.record_label(1, "approved", "human")
    monkeypatch.setattr("foragent.watch_open.open_view", lambda *args, **kwargs: None)
    original = case.tracker.add_labels
    failures = ["gh: http2: cannot retry err [stream error: REFUSED_STREAM] after Request.Body was written"]

    def add_labels(number, labels, *args, **kwargs):
        # GitHub applied the label, then the connection failed before the reply
        # (seen as "REFUSED_STREAM ... after Request.Body was written").
        result = original(number, labels, *args, **kwargs)
        if "running" in labels and failures:
            raise TrackerError(failures.pop())
        return result

    monkeypatch.setattr(case.tracker, "add_labels", add_labels)
    launched = []
    ctx = replace(tick_context(case), launcher=lambda n: launched.append(n) or os.getpid())  # a live pid
    try:
        tick(ctx)
    except TrackerError:
        pass  # A tick may stop on the error; the next tick must recover cleanly.
    tick(ctx)
    rows = [json.loads(line) for line in (case.store.root / "runs.jsonl").read_text().splitlines()
            if line.strip()] if (case.store.root / "runs.jsonl").exists() else []
    assert not [row for row in rows if row["number"] == 1 and row.get("outcome") == "crashed"], (
        "A GitHub error before the runner launched is not a runner crash")
    issue = case.tracker.get_issue(1)
    assert not issue.labels & {"blocked", "needs-human"}
    assert launched == [1], "The issue must start exactly once"
    run = case.store.get_run(1)
    assert run is not None and run.status == "running" and run.pid == os.getpid()
    assert "running" in issue.labels


def test_run_failure_reasons_rate_limit_during_publish_waits_instead_of_failing(
    git_history, tmp_path, monkeypatch,
):
    from test_github_limit_wait import github_case

    from foragent.runner import run_issue
    from foragent.state import StateStore

    case = github_case(git_history, tmp_path, monkeypatch, attempts=1)
    case.error_pending = False
    implement = case.implementer.steps[0]

    def implement_then_limit(kwargs):
        result = implement(kwargs) if callable(implement) else implement
        case.error_pending = True  # The next issue read, at publish time, hits the limit.
        return result

    case.implementer.steps = [implement_then_limit]
    outcome = run_issue(1, case.ctx)
    assert case.errors, "The rate-limit error must be raised after the implementation"
    run = StateStore(case.store.root, now=lambda: case.clock[0]).get_run(1)
    assert outcome not in {"error", "retries-exceeded", "blocked"}
    assert run.waiting_until is not None
    assert case.store.load_flag(1) is None
    assert not case.tracker.get_issue(1).labels & {"blocked", "needs-human"}
