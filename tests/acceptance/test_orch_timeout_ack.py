"""Acceptance test for orch-timeout-ack. Locked by Foragent; do not edit."""

from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from foragent import cli
from foragent.config import Config
from foragent.scheduler import TickContext, tick
from foragent.state import StateStore
from foragent.tracker import FakeTracker


TIMEOUT = "<!-- foragent:needs-human reason=orchestrator-timeout -->"


@pytest.fixture
def case(tmp_path, monkeypatch):
    """Fake only the clock, tracker, Git transport and CLI dependency wiring."""
    root = tmp_path / "project"
    root.mkdir()
    clock = [datetime(2026, 10, 31, 9, tzinfo=UTC)]
    config = Config(
        repo="owner/repo", language="en", skill_libraries=[], notify="none",
        orchestrator_timeout_minutes=30, run_hours="00-01",
    )
    tracker = FakeTracker(now=lambda: clock[0], strict=True)
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    git = SimpleNamespace(fetch=lambda *args: None)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0] if tz else clock[0].replace(tzinfo=None)

    monkeypatch.setattr(cli, "datetime", Clock)
    monkeypatch.setattr(cli, "load_config", lambda root: config)
    monkeypatch.setattr(cli, "GhTracker", lambda repo: tracker)
    monkeypatch.setattr(cli, "GitRepo", lambda root: git)
    monkeypatch.setattr(StateStore, "for_repo", classmethod(lambda cls, repo, **kwargs: store))

    def forbidden(*args, **kwargs):
        pytest.fail("This acceptance flow must stay offline and must not launch a worker")

    from foragent import gh
    import socket

    monkeypatch.setattr(gh, "run", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    context = TickContext(
        repo="owner/repo", repo_root=root, tracker=tracker, store=store,
        config=config, git=git, now=lambda: clock[0], launcher=forbidden,
        start_runs=False,
    )
    return SimpleNamespace(root=root, clock=clock, tracker=tracker, store=store, context=context)


def block(case, number, *, reason="review-dispute", details="Review findings need an orchestrator look."):
    case.tracker.add_issue(number, f"Blocked work {number}", labels=["blocked"])
    case.tracker.comment(number, f"<!-- foragent:blocked reason={reason} -->\n{details}")


def note(case, number, why):
    assert cli.main([
        "orch", "note", "--repo-root", str(case.root),
        "--now", f"Diagnosed blocker #{number}", "--why", why,
        "--next", f"Wait for the recorded follow-up for #{number}",
    ]) == 0, "The public orchestrator note command must succeed"


def run_tick(case):
    report = tick(case.context)
    assert report.skipped is None, report


def timeout_comments(case, number):
    return [comment for comment in case.tracker.list_comments(number) if comment.body.startswith(TIMEOUT)]


def inbox(case, capsys):
    capsys.readouterr()
    assert cli.main(["inspect", "--repo-root", str(case.root), "--json"]) == 0
    return json.loads(capsys.readouterr().out)["items"]


@pytest.mark.parametrize("reason,why", [
    ("review-dispute", "The reviewer findings are diagnosed; waiting for the recorded technical follow-up."),
    ("retries-exceeded", "The same time-dependent unit test fails on base/main and the issue head; waiting for fix #1295 to merge."),
], ids=["review-block", "same-failure-on-main"])
def test_acknowledged_blocker_stays_out_of_human_inbox_after_thirty_minutes(case, capsys, reason, why):
    block(case, 1, reason=reason, details=why)
    block(case, 2)
    case.clock[0] += timedelta(minutes=1)
    note(case, 1, why)
    case.clock[0] += timedelta(minutes=1)
    # Other work must neither erase #1's acknowledgement nor acknowledge #2.
    case.tracker.add_issue(3, "Other work")
    note(case, 3, "An unrelated task is being monitored.")
    case.clock[0] += timedelta(minutes=31)

    for elapsed in (timedelta(), timedelta(hours=2)):
        case.clock[0] += elapsed
        run_tick(case)
        assert case.tracker.get_issue(1).labels == {"blocked"}, (
            "An acknowledgement of the current blocker remains effective after 30 minutes"
        )
        assert timeout_comments(case, 1) == []
        assert "needs-human" in case.tracker.get_issue(2).labels, (
            "Acknowledging a different issue must not silence this unattended blocker"
        )
        assert len(timeout_comments(case, 2)) == 1
        assert [item["number"] for item in inbox(case, capsys)] == [2]


def test_unacknowledged_blocker_times_out_once_and_inbox_line_calls_it_a_timeout(case, capsys):
    block(case, 1)
    case.clock[0] += timedelta(minutes=29)
    run_tick(case)
    assert case.tracker.get_issue(1).labels == {"blocked"}
    assert timeout_comments(case, 1) == []
    assert inbox(case, capsys) == []

    case.clock[0] += timedelta(minutes=2)
    run_tick(case)
    run_tick(case)
    assert "needs-human" in case.tracker.get_issue(1).labels
    assert len(timeout_comments(case, 1)) == 1, "A timeout must be recorded exactly once"
    item, = inbox(case, capsys)
    assert (item["number"], item["reason"]) == (1, "orchestrator-timeout")
    assert item["group"] != "decision"

    assert cli.main(["inspect", "--repo-root", str(case.root)]) == 0
    lines = capsys.readouterr().out.splitlines()
    line, = [line for line in lines if "#1 " in line and " — " in line]
    assert "timeout" in line.casefold(), f"The person's inbox line must name the timeout: {line}"
    assert "decision" not in line.casefold(), line


def test_acknowledgement_before_latest_block_does_not_cover_the_new_blocker(case, capsys):
    block(case, 1)
    case.clock[0] += timedelta(minutes=1)
    note(case, 1, "The first review findings are diagnosed.")
    case.clock[0] += timedelta(minutes=1)
    # A later blocker needs a fresh look, even though this issue was acknowledged.
    case.tracker.comment(1, "<!-- foragent:blocked reason=spec-question -->\nA new blocker appeared.")

    case.clock[0] += timedelta(minutes=29)
    run_tick(case)
    assert case.tracker.get_issue(1).labels == {"blocked"}, "Use the latest blocker's timeout window"
    assert timeout_comments(case, 1) == []
    assert inbox(case, capsys) == []

    case.clock[0] += timedelta(minutes=2)
    run_tick(case)
    run_tick(case)
    assert "needs-human" in case.tracker.get_issue(1).labels, (
        "An acknowledgement older than the latest block is not a response to that block"
    )
    assert len(timeout_comments(case, 1)) == 1
    item, = inbox(case, capsys)
    assert (item["number"], item["reason"]) == (1, "orchestrator-timeout")
