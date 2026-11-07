"""Unit checks for orch_promise: PR events the local records do not hold, owners, owner prompts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from foragent import orch_inbox, orch_promise
from foragent.state import StateStore
from foragent.tracker import Issue, OwnerPullRequest, TrackerError

NOW = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)


class PRTracker:
    """GitHub as the tick reads it: #77 is a PR outside foragent/, #5 an issue."""

    def __init__(self):
        self.pr = {"state": "open", "merged_at": None}
        self.reads = 0

    def get_issue(self, n):
        if n == 77:
            return Issue(77, "PR", "", frozenset(), self.pr["state"], is_pull_request=True)
        if n == 5:
            return Issue(5, "Issue", "", frozenset(), "open")
        raise TrackerError(f"#{n}: HTTP 404")

    def get_pr(self, n):
        self.reads += 1
        if n != 77:
            raise TrackerError(f"PR #{n}: HTTP 404")
        return OwnerPullRequest(77, "jin/side", "main", "PR", "", self.pr["merged_at"], "", self.pr["state"])


@pytest.fixture
def store(tmp_path):
    clock = [NOW]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    store.clock = clock
    return store


def tick(store, tracker, minutes=5):
    store.clock[0] += timedelta(minutes=minutes)
    return orch_promise.on_tick(SimpleNamespace(store=store, tracker=tracker, now=lambda: store.clock[0]))


def state(store, promise_id):
    return next(row["state"] for row in orch_promise.promises(store) if row["id"] == promise_id)


@pytest.mark.parametrize("kind, change", [
    ("closed", {"state": "closed"}),
    ("merged", {"state": "closed", "merged_at": "2026-11-07T09:07:00Z"}),
])
def test_a_pr_event_outside_the_local_records_becomes_due_from_a_github_read(store, kind, change):
    tracker = PRTracker()
    record = orch_promise.add(store, tracker, f"{kind}:77", "publish after the PR", "orchestrator", NOW)
    assert record["target_type"] == "pr"
    tick(store, tracker)
    assert state(store, record["id"]) == "waiting"
    tracker.pr.update(change)
    tick(store, tracker)
    assert state(store, record["id"]) == "due"
    assert [row["promise"] for row in orch_inbox.requests(store) if row.get("kind") == "promise-due"] == [record["id"]]


def test_a_closed_unmerged_pr_does_not_make_a_merged_promise_due(store):
    tracker = PRTracker()
    record = orch_promise.add(store, tracker, "merged:77", "publish after the PR", "orchestrator", NOW)
    tracker.pr["state"] = "closed"
    tick(store, tracker)
    assert state(store, record["id"]) == "waiting"


def test_pr_reads_wait_while_github_is_paused_and_are_one_per_tick(store):
    tracker = PRTracker()
    for _ in range(2):
        orch_promise.add(store, tracker, "closed:77", "publish after the PR", "orchestrator", NOW)
    store.pause_github(NOW + timedelta(hours=1))
    tick(store, tracker)
    assert tracker.reads == 0, "No GitHub read while GitHub is paused"
    tick(store, tracker, minutes=70)
    assert tracker.reads == 1, "Two promises on one PR share one read per tick"


def test_approved_on_a_pr_and_unknown_owners_are_refused(store):
    tracker = PRTracker()
    with pytest.raises(orch_promise.PromiseError, match="approved:77"):
        orch_promise.add(store, tracker, "approved:77", "x", "orchestrator", NOW)
    with pytest.raises(orch_promise.PromiseError, match="orchestrater"):
        orch_promise.add(store, tracker, "closed:5", "x", "orchestrater", NOW, known_owners={"orchestrator", "pm"})
    assert orch_promise.promises(store) == []


def test_owners_are_pm_orchestrator_lanes_and_roles():
    config = SimpleNamespace(lanes={"roles": {}}, roles={"ux-researcher": {"memory": "m"}})
    found = orch_promise.owners(config)
    assert {"pm", "orchestrator", "main", "roles", "ux-researcher"} <= found


def test_the_orchestrators_first_message_lists_its_due_promises(store, tmp_path, monkeypatch):
    from foragent import orch_spawn, orch_spawn_launch, session_skills

    tracker = PRTracker()
    mine = orch_promise.add(store, tracker, "closed:77", "publish order sky", "orchestrator", NOW)
    orch_promise.add(store, tracker, "closed:77", "rerun prepare", "pm", NOW)
    tracker.pr["state"] = "closed"
    tick(store, tracker)
    seen = []
    monkeypatch.setattr(session_skills, "section", lambda *args, **kwargs: "")
    monkeypatch.setattr(orch_spawn, "_command", lambda root, store, title, sid, first, config=None:
                        seen.append(first) or ("screen", ["claude"]))
    orch_spawn_launch.plan_launch(tmp_path, store, SimpleNamespace(), NOW, "/fa-orchestrator")
    assert "publish order sky" in seen[0] and mine["id"] in seen[0]
    assert "rerun prepare" not in seen[0]


def test_role_prompt_section_lists_only_that_owners_due_promises(store):
    tracker = PRTracker()
    orch_promise.add(store, tracker, "closed:77", "split the module", "architect", NOW)
    orch_promise.add(store, tracker, "closed:5", "later work", "architect", NOW)
    assert orch_promise.prompt_section(store, "architect") == ""
    tracker.pr["state"] = "closed"
    tick(store, tracker)
    section = orch_promise.prompt_section(store, "architect")
    assert "split the module" in section and "later work" not in section
