"""Gate alarms that call the architect directly (#1469): pending retries, worse-only rearming,
finished-dive completion and the waiting-on-person source."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from foragent import orch_architect, orch_facts_kinds, orch_inbox, orch_role_raise, orch_roles
from foragent.state import StateStore
from foragent.tracker import Issue

NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)
KIND = "lane_concentration"
SIGNAL = {"paths": [], "kinds": [KIND]}


@pytest.fixture
def store(tmp_path):
    clock = [NOW]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    store.clock = clock
    return store


@pytest.fixture
def alarm(monkeypatch, tmp_path, store):
    """The architect, configured, with one lane_concentration signal and a fake session."""
    state = SimpleNamespace(kinds={KIND}, numbers={KIND: {"runs": 18, "lane": "roles", "lane_runs": 16}},
                            calls=[], refuse=None, alive=False)
    (tmp_path / ".foragent.yml").write_text("repo: owner/repo\n", encoding="utf-8")
    config = SimpleNamespace(roles={"architect": {}}, language="en")
    monkeypatch.setattr(orch_role_raise, "load_config", lambda root: config)
    monkeypatch.setattr(orch_role_raise, "load_roles", lambda config: {"architect": {"signals": [dict(SIGNAL)]}})
    monkeypatch.setattr(orch_roles, "observe",
                        lambda store, config, root, roles: (set(), state.kinds, dict(state.numbers), None))

    def call(root, store, config, why, now, role, alarm):
        if state.refuse is not None:
            raise state.refuse
        state.calls.append(alarm)
        return "started"

    monkeypatch.setattr(orch_architect, "call", call)
    monkeypatch.setattr(orch_architect, "session_alive", lambda record: state.alive)

    def tick(minutes=5, start=True):
        store.clock[0] += timedelta(minutes=minutes)
        if start:
            return orch_role_raise.raise_due(store, tmp_path, store.clock[0], start=True)
        return orch_role_raise.raise_due(store, tmp_path, store.clock[0])

    state.tick = tick
    return state


def rows(store):
    return orch_role_raise.signal_requests(store)


def test_a_refused_launch_stays_pending_and_is_called_after_recovery(store, alarm):
    alarm.refuse = orch_architect.ArchitectRefused("executor paused")
    alarm.tick()
    assert alarm.calls == [] and [row["status"] for row in rows(store)] == ["new"]
    alarm.refuse = None
    alarm.tick()
    [row] = rows(store)
    assert alarm.calls == [row["id"]], "The executor recovered: the pending alarm must launch once"


@pytest.mark.parametrize("failure", [OSError("cannot start"), RuntimeError("record write")])
def test_a_launch_exception_keeps_the_alarm_pending(store, alarm, failure):
    alarm.refuse = failure
    alarm.tick()
    alarm.refuse = None
    alarm.tick()
    assert alarm.calls == [rows(store)[0]["id"]] and len(rows(store)) == 1


def test_an_alarm_recorded_by_a_no_start_tick_is_called_on_the_next_start_tick(store, alarm):
    alarm.tick(start=False)
    assert alarm.calls == []
    alarm.tick()
    assert alarm.calls == [rows(store)[0]["id"]]


def test_a_failed_or_missing_session_retries_only_after_the_retry_window(store, alarm):
    alarm.tick()
    [row] = rows(store)
    orch_architect._deliver(store, "architect", row["id"], 1, "crashed before digging")
    assert rows(store)[0]["status"] != "done", "A failed dive must not answer the alarm"
    alarm.tick()
    assert alarm.calls == [row["id"]], "No retry inside the retry window"
    alarm.tick(minutes=int(orch_role_raise.RETRY_AFTER.total_seconds() // 60))
    assert alarm.calls == [row["id"], row["id"]], "The same alarm retries once the window passed"
    orch_architect._deliver(store, "architect", row["id"], 0, "dug to the cause")
    [done] = rows(store)
    assert done["status"] == "done" and done["answer"] == "called"
    assert done["answer_facts"] == done["call_facts"]
    alarm.tick(minutes=120)
    assert len(alarm.calls) == 2, "A finished dive covers its numbers"


def test_a_finished_dive_answers_with_the_numbers_at_its_end(store, alarm, tmp_path):
    """The baseline is what the finished dive left, not what its launch saw."""
    alarm.numbers[KIND]["lane_runs"] = 17
    alarm.tick()
    alarm.numbers[KIND]["lane_runs"] = 15
    orch_role_raise.complete_call(store, rows(store)[0]["id"], tmp_path)
    assert rows(store)[0]["answer_facts"]["kinds"][KIND]["numbers"]["lane_runs"] == 15
    alarm.numbers[KIND]["lane_runs"] = 16
    alarm.tick()
    assert len(alarm.calls) == 2, "16 is worse than the 15 the dive ended with, though the launch saw 17"


def test_a_live_session_is_never_duplicated(store, alarm):
    alarm.alive = True
    alarm.tick()
    assert alarm.calls == [] and rows(store) == []


def test_a_clear_and_return_with_the_same_numbers_stays_quiet(store, alarm):
    alarm.tick()
    orch_role_raise.complete_call(store, rows(store)[0]["id"])
    alarm.kinds = set()
    alarm.tick()
    assert rows(store)[0].get("cleared_at")
    alarm.kinds = {KIND}
    for _ in range(2):
        alarm.tick()
    assert len(alarm.calls) == 1, "A clear and return is not a worse burden"
    alarm.numbers[KIND]["lane_runs"] = 17
    alarm.tick()
    assert len(alarm.calls) == 2, "A larger share is"


@pytest.mark.parametrize("numbers,called", [
    ({"runs": 19, "lane": "roles", "lane_runs": 16}, False),
    ({"runs": 30, "lane": "roles", "lane_runs": 17}, False),
    ({"runs": 18, "lane": "roles", "lane_runs": 17}, True),
    ({"runs": 18, "lane": "view", "lane_runs": 16}, True),
], ids=["more-runs-only", "diluted-share", "larger-share", "other-lane"])
def test_lane_concentration_compares_the_share_not_the_total(store, alarm, numbers, called):
    alarm.tick()
    orch_role_raise.complete_call(store, rows(store)[0]["id"])
    alarm.numbers[KIND] = numbers
    alarm.tick()
    assert (len(alarm.calls) == 2) is called


def test_context_and_elapsed_numbers_and_a_normal_binding_are_no_burden():
    def facts(kind, numbers, binding=None):
        item = {"numbers": numbers, **({"binding": binding} if binding else {})}
        return orch_role_raise.burden({"kinds": {kind: item}})

    worse = orch_role_raise.worse
    assert not worse(facts("files_over_limit", {"limit": 500, "lines_by_file": {"a": 600}}),
                     facts("files_over_limit", {"limit": 550, "lines_by_file": {"a": 600}}))
    assert not worse(facts("merge_stall", {"queued": 2, "minutes_since_merge": 30, "merge_interval_minutes": 10}),
                     facts("merge_stall", {"queued": 2, "minutes_since_merge": 90, "merge_interval_minutes": 5}))
    assert worse(facts("merge_stall", {"queued": 2}), facts("merge_stall", {"queued": 3}))
    assert not worse(facts("waiting_on_person", {"waiting": 2, "oldest": "#7", "oldest_minutes": 60}),
                     facts("waiting_on_person", {"waiting": 2, "oldest": "#7", "oldest_minutes": 900}))
    assert worse(facts("waiting_on_person", {"waiting": 2, "oldest": "#7"}),
                 facts("waiting_on_person", {"waiting": 2, "oldest": "#8"}))
    assert worse(facts("waiting_on_person", {"waiting": 2, "oldest": "#7"}),
                 facts("waiting_on_person", {"waiting": 3, "oldest": "#7"}))
    idle = {"idle_lanes": ["a"], "ready_by_lane": {"b": 3}}
    assert not worse(facts("idle_lanes_with_backlog", idle, {"kind": "cap", "normal": False}),
                     facts("idle_lanes_with_backlog", idle, {"kind": "cap", "normal": True}))
    assert worse(facts("idle_lanes_with_backlog", idle, {"kind": "cap", "normal": True}),
                 facts("idle_lanes_with_backlog", idle, {"kind": "cap", "normal": False}))


MARKERS = {
    "needs-human": "<!-- foragent:needs-human reason=escalated -->\nNeeds a decision",
    "waiting-person": "<!-- foragent:orch kind=parked -->\nWaiting for Jin",
}


def hand_off(store, number, label, at, comment_id=1):
    """An issue observed with a person label and the marker comment that set it at ``at``."""
    store.record_issue(Issue(number, "Needs a decision", "", frozenset({label}), "open", False), {})
    store.record_comments(number, [{"id": comment_id, "body": MARKERS[label], "created_at": at.isoformat()}])


@pytest.mark.parametrize("label", orch_facts_kinds.PERSON_LABELS)
def test_a_labelled_issue_waits_from_its_marker_comment(store, label):
    hand_off(store, 7, label, NOW)
    assert orch_facts_kinds.waiting_on_person_numbers(store, NOW + timedelta(minutes=50)) == {}
    later = NOW + timedelta(days=2)
    assert orch_facts_kinds.waiting_on_person_numbers(store, later) == {
        "waiting": 1, "oldest": "#7", "oldest_minutes": 2 * 24 * 60}
    store.record_issue(Issue(7, "Needs a decision", "", frozenset(), "open", False), {})
    assert orch_facts_kinds.waiting_on_person_numbers(store, later) == {}, "The hand-off ended"


@pytest.mark.parametrize("label", orch_facts_kinds.PERSON_LABELS)
def test_the_wait_start_survives_observations_and_flow_rotation(store, label, monkeypatch):
    """Observations and retained-log rotation never move an unchanged item's wait start."""
    hand_off(store, 7, label, NOW)
    store.clock[0] = NOW + timedelta(hours=1)
    orch_inbox.ask(store, "Which option?", ["a", "b"], 1)
    later = NOW + timedelta(days=2)
    expected = {"waiting": 2, "oldest": "#7", "oldest_minutes": 2 * 24 * 60}
    for hours in (2, 30):
        store.clock[0] = NOW + timedelta(hours=hours)
        store.record_issue(Issue(7, "Needs a decision", f"edit {hours}", frozenset({label}), "open", False), {})
        assert orch_facts_kinds.waiting_on_person_numbers(store, later) == expected
    monkeypatch.setattr(type(store), "flow_records", lambda self, **kwargs: [])
    assert orch_facts_kinds.waiting_on_person_numbers(store, later) == expected


def test_open_questions_and_labelled_issues_count_together(store):
    hand_off(store, 7, "needs-human", NOW)
    store.clock[0] = NOW + timedelta(hours=1)
    question = orch_inbox.ask(store, "Which option?", ["a", "b"], 1)
    numbers = orch_facts_kinds.waiting_on_person_numbers(store, NOW + timedelta(hours=3))
    assert numbers == {"waiting": 2, "oldest": "#7", "oldest_minutes": 180}
    orch_inbox.answer(store, question["id"], "a")
    assert orch_facts_kinds.waiting_on_person_numbers(store, NOW + timedelta(hours=3))["waiting"] == 1


def test_a_hand_set_label_counts_but_never_becomes_the_oldest(store):
    store.record_issue(Issue(8, "Labelled by hand", "", frozenset({"needs-human"}), "open", False), {})
    assert orch_facts_kinds.waiting_on_person_numbers(store, NOW + timedelta(days=2)) == {}
    hand_off(store, 7, "needs-human", NOW)
    assert orch_facts_kinds.waiting_on_person_numbers(store, NOW + timedelta(hours=2)) == {
        "waiting": 2, "oldest": "#7", "oldest_minutes": 120}
