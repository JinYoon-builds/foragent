"""Unit checks for orch_promise: only events the gate observes are accepted, refusals name them."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from foragent import orch_inbox, orch_promise
from foragent.state import StateStore
from foragent.tracker import Issue, OwnerPullRequest, TrackerError

NOW = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)
SUPPORTED = ("closed:", "merged:", "approved:", "time:")


class PRTracker:
    """GitHub as `add` reads it: #77 a PR on jin/side, #78 a PR on foragent/issue-5, #5 an issue."""

    def get_issue(self, n):
        if n in (77, 78):
            return Issue(n, "PR", "", frozenset(), "open", is_pull_request=True)
        if n == 5:
            return Issue(5, "Issue", "", frozenset(), "open")
        raise TrackerError(f"#{n}: HTTP 404")

    def get_pr(self, n):
        head = {77: "jin/side", 78: "foragent/issue-5"}[n]
        return OwnerPullRequest(n, head, "main", "PR", "", None, "", "open")


@pytest.fixture
def store(tmp_path):
    clock = [NOW]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    store.clock = clock
    return store


def tick(store, minutes=5):
    store.clock[0] += timedelta(minutes=minutes)
    return orch_promise.on_tick(SimpleNamespace(store=store, now=lambda: store.clock[0]))


def refused(store, when, owner="orchestrator", **kwargs):
    with pytest.raises(orch_promise.PromiseError) as caught:
        orch_promise.add(store, PRTracker(), when, "publish the next order", owner, NOW, **kwargs)
    return str(caught.value)


@pytest.mark.parametrize("when", ["closed:77", "closed:78", "approved:77", "merged:77", "merged:98765",
                                  "time:tomorrow", "pr-closed:77", "when CI is green"])
def test_events_the_gate_does_not_observe_are_refused_naming_the_supported_events(store, when):
    message = refused(store, when)
    assert [kind for kind in SUPPORTED if kind not in message] == [], message
    assert orch_promise.promises(store) == [], "A refused promise is not recorded"


def test_a_merged_foragent_pr_is_accepted_and_becomes_due_from_the_merged_listing(store):
    record = orch_promise.add(store, PRTracker(), "merged:78", "publish order sky", "orchestrator", NOW)
    tick(store)
    assert orch_promise.promises(store)[0]["state"] == "waiting"
    store.record_merged_pr_listing({"watermark": NOW.isoformat(), "prs": [
        {"number": 78, "head": "foragent/issue-5", "base": "main", "merged_at": NOW.isoformat(), "url": ""}]})
    tick(store)
    assert orch_promise.promises(store)[0]["state"] == "due"
    assert [row["promise"] for row in orch_inbox.requests(store) if row.get("kind") == "promise-due"] \
        == [record["id"]]


def test_unknown_owners_are_refused(store):
    message = refused(store, "closed:5", owner="orchestrater", known_owners={"orchestrator", "pm"})
    assert "orchestrater" in message
    assert orch_promise.promises(store) == []


def test_owners_are_pm_orchestrator_lanes_and_roles():
    config = SimpleNamespace(lanes={"roles": {}}, roles={"ux-researcher": {"memory": "m"}})
    assert {"pm", "orchestrator", "main", "roles", "ux-researcher"} <= orch_promise.owners(config)


def test_the_shelf_place_counts_open_promises(store):
    from foragent import shelf

    orch_promise.add(store, PRTracker(), "closed:5", "later work", "orchestrator", NOW)
    assert "promises" in {place.name for place in shelf.places()}
    assert shelf.summary(store)["promises"]["count"] == 1
