"""Boundaries of the idle-lanes wake request (#2842): every kind of lane run counts as work.

A lane works when it runs an issue, a dream or a test draft. Each of these must hold the request
back while it is live, and its start and finish must open a new idle stretch afterwards.
"""

from datetime import UTC, datetime, timedelta

import pytest

from foragent import orch_facts_stall, orch_inbox, scheduler_idle
from foragent.state import StateStore

WAITING = [741, 863]


@pytest.fixture
def idle(tmp_path, monkeypatch):
    clock = [datetime(2026, 11, 8, 5, 7, tzinfo=UTC)]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    monkeypatch.setattr(orch_facts_stall, "unapproved_ready_numbers", lambda _store: list(WAITING))

    def step(minutes=5):
        clock[0] += timedelta(minutes=minutes)
        return scheduler_idle.tick(store)

    return store, step


def _requests(store):
    return [row for row in orch_inbox.requests(store) if row.get(scheduler_idle.FIELD)]


def _issue(store):
    store.start_run(900, branch="foragent/issue-900", worktree=store.worktree_path(900), lane="kestrel")
    return lambda: store.finish_run(900, outcome="cancelled")


def _dream(store):
    store.start_dream("kestrel", worktree=store.root / "dream")
    return lambda: store.finish_dream("kestrel", "done")


def _draft(store):
    store.start_draft(905, "kestrel", worktree=store.root / "draft")
    return lambda: store.finish_run("draft-905", "drafted")


@pytest.mark.parametrize("start", [_issue, _dream, _draft], ids=["issue", "dream", "draft"])
def test_a_live_lane_run_of_any_kind_holds_the_request_and_its_end_opens_a_new_stretch(idle, start):
    store, step = idle
    first = step()
    assert first is not None and first["issues"] == WAITING
    assert "#741" in first["text"] and "#863" in first["text"]
    assert step() is None, "the same idle stretch must not add a second request"

    finish = start(store)
    assert step() is None, "a live lane run means not every lane is idle"
    assert len(_requests(store)) == 1

    finish()
    second = step()
    assert second is not None, "the run's end starts a new idle stretch, which adds one request"
    assert second[scheduler_idle.FIELD] != first[scheduler_idle.FIELD]
    assert step() is None
    assert len(_requests(store)) == 2


def test_an_answered_request_still_counts_for_its_stretch(idle):
    store, step = idle
    request = step()
    orch_inbox.set_request_status(store, request["id"], "done", "Preread")
    assert step() is None
    assert len(_requests(store)) == 1


def test_no_waiting_issue_adds_nothing(idle, monkeypatch):
    store, step = idle
    monkeypatch.setattr(orch_facts_stall, "unapproved_ready_numbers", lambda _store: [])
    assert step() is None
    assert _requests(store) == []


def test_other_files_under_runs_do_not_move_the_stretch(idle):
    store, step = idle
    first = step()
    (store.root / "runs").mkdir(parents=True, exist_ok=True)
    (store.root / "runs" / "issue-7-local.json").write_text('{"started_at": "2030-01-01T00:00:00+00:00"}',
                                                             encoding="utf-8")
    assert step() is None
    assert scheduler_idle.stretch(store) == first[scheduler_idle.FIELD]
