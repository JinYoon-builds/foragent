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
    assert orch_facts_kinds.waiting_on_person_numbers(store, NOW + timedelta(minutes=50)) == {
        "waiting": 1, "oldest": "#7", "items": ["#7"], "oldest_minutes": 50}, "Present while anyone waits"
    later = NOW + timedelta(days=2)
    assert orch_facts_kinds.waiting_on_person_numbers(store, later) == {
        "waiting": 1, "oldest": "#7", "items": ["#7"], "oldest_minutes": 2 * 24 * 60}
    store.record_issue(Issue(7, "Needs a decision", "", frozenset(), "open", False), {})
    assert orch_facts_kinds.waiting_on_person_numbers(store, later) == {}, "The hand-off ended"


@pytest.mark.parametrize("label", orch_facts_kinds.PERSON_LABELS)
def test_the_wait_start_survives_observations_and_flow_rotation(store, label, monkeypatch):
    """Observations and retained-log rotation never move an unchanged item's wait start."""
    hand_off(store, 7, label, NOW)
    store.clock[0] = NOW + timedelta(hours=1)
    question = orch_inbox.ask(store, "Which option?", ["a", "b"], 1)
    later = NOW + timedelta(days=2)
    expected = {"waiting": 2, "oldest": "#7", "items": sorted(["#7", question["id"]]),
                "oldest_minutes": 2 * 24 * 60}
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
    assert numbers == {"waiting": 2, "oldest": "#7", "items": sorted(["#7", question["id"]]),
                       "oldest_minutes": 180}
    orch_inbox.answer(store, question["id"], "a")
    assert orch_facts_kinds.waiting_on_person_numbers(store, NOW + timedelta(hours=3))["waiting"] == 1


def test_a_hand_set_label_counts_but_never_becomes_the_oldest(store):
    store.record_issue(Issue(8, "Labelled by hand", "", frozenset({"needs-human"}), "open", False), {})
    assert orch_facts_kinds.waiting_on_person_numbers(store, NOW + timedelta(days=2)) == {
        "waiting": 1, "oldest": None, "items": ["#8"], "oldest_minutes": None}, (
        "An item whose comments were never read still waits on a person")
    hand_off(store, 7, "needs-human", NOW)
    assert orch_facts_kinds.waiting_on_person_numbers(store, NOW + timedelta(hours=2)) == {
        "waiting": 2, "oldest": "#7", "items": ["#7", "#8"], "oldest_minutes": 120}


def test_the_named_oldest_holds_while_it_still_waits():
    def facts(numbers):
        return orch_role_raise.burden({"kinds": {"waiting_on_person": {"numbers": numbers}}})

    before = facts({"waiting": 2, "oldest": "#7", "items": ["#7", "q1"], "oldest_minutes": 300})
    worse = orch_role_raise.worse_facts
    assert not worse(before, facts({"waiting": 2, "oldest": "q1", "items": ["#7", "q1"]})), "Same waiters"
    assert not worse(before, facts({"waiting": 2, "oldest": "#7", "items": ["#7", "#9"]})), (
        "A replaced newer waiter neither grows the count nor changes the oldest")
    assert worse(before, facts({"waiting": 2, "oldest": "q1", "items": ["q1", "#9"]})), "The oldest stopped waiting"
    assert worse(before, facts({"waiting": 3, "oldest": "#7", "items": ["#7", "q1", "#9"]})), "More waiters"
    assert not worse(before, facts({"waiting": 1, "oldest": "#7", "items": ["#7"]}))
    raw = lambda numbers: {"kinds": {"waiting_on_person": {"numbers": numbers}}}  # noqa: E731
    first = raw({"waiting": 2, "oldest": "#7", "items": ["#7", "q1"], "oldest_minutes": 300})
    assert not orch_role_raise.new_evidence(first, raw({"waiting": 2, "oldest": "q1", "items": ["#7", "q1"]})), (
        "A no-start tick reads the same waiters the same way")
    assert orch_role_raise.new_evidence(first, raw({"waiting": 2, "oldest": "q1", "items": ["q1", "#9"]}))


@pytest.mark.parametrize("label", orch_facts_kinds.PERSON_LABELS)
def test_delivering_a_queued_marker_never_calls_again(store, alarm, tmp_path, label):
    """Review round 3: an acknowledged marker moves its time, not the people who wait."""
    store.record_issue(Issue(7, "Needs a decision", "", frozenset({label}), "open", False), {})
    entry = store.queue_github_write(7, "comment", body=MARKERS[label])
    store.clock[0] = NOW + timedelta(minutes=30)
    question = orch_inbox.ask(store, "Which option?", ["a", "b"], 1)
    alarm.kinds = {"waiting_on_person"}

    def measure():
        alarm.numbers = {"waiting_on_person": orch_facts_kinds.waiting_on_person_numbers(store, store.clock[0])}
        return alarm.numbers["waiting_on_person"]

    store.clock[0] = NOW + timedelta(hours=2)
    assert measure()["oldest"] == "#7"
    alarm.tick(minutes=0)
    [request] = [row for row in rows(store) if "waiting_on_person" in row["signal"]["kinds"]]
    orch_role_raise.complete_call(store, request["id"], tmp_path)
    store.acknowledge_github_comment(entry, 501)
    store.clock[0] = NOW + timedelta(hours=3)
    assert measure()["oldest"] == question["id"], "The delivered marker is dated later than the question"
    alarm.tick()
    assert len(alarm.calls) == 1, "The same two waiters must stay quiet"
    store.record_issue(Issue(7, "Needs a decision", "", frozenset(), "open", False), {})
    store.record_issue(Issue(9, "Another", "", frozenset({label}), "open", False), {})
    measure()
    alarm.tick()
    assert len(alarm.calls) == 2, "The oldest named at the answer stopped waiting"


@pytest.mark.parametrize("label", orch_facts_kinds.PERSON_LABELS)
@pytest.mark.parametrize("path", ["completion", "signal-answer"])
def test_a_marker_delivered_just_before_the_answer_never_calls_again(store, alarm, tmp_path, label, path):
    """Review round 4: delivery resets a marker's age; the answer still covers the same waiters."""
    store.record_issue(Issue(7, "Needs a decision", "", frozenset({label}), "open", False), {})
    entry = store.queue_github_write(7, "comment", body=MARKERS[label])
    store.clock[0] = NOW + timedelta(minutes=30)
    orch_inbox.ask(store, "Which option?", ["a", "b"], 1)
    alarm.kinds = {"waiting_on_person"}

    def measure():
        alarm.numbers = {"waiting_on_person": orch_facts_kinds.waiting_on_person_numbers(store, store.clock[0])}

    store.clock[0] = NOW + timedelta(hours=2)
    measure()
    alarm.tick(minutes=0, start=path == "completion")
    [request] = [row for row in rows(store) if "waiting_on_person" in row["signal"]["kinds"]]
    store.acknowledge_github_comment(entry, 501)
    measure()
    if path == "completion":
        orch_role_raise.complete_call(store, request["id"], tmp_path)
    else:
        orch_role_raise.answer(store, request["id"], "not-now", "seen",
                               orch_role_raise.measure(store, tmp_path, request["id"]))
    answered = next(row for row in rows(store) if row["id"] == request["id"])
    assert answered["answer_facts"]["kinds"]["waiting_on_person"]["numbers"]["waiting"] == 2
    store.clock[0] += timedelta(minutes=65)
    measure()
    calls = len(alarm.calls)
    alarm.tick()
    assert len(alarm.calls) == calls, "The same two waiters must stay quiet"


@pytest.mark.parametrize("path", ["completion", "signal-answer"])
def test_an_answer_while_the_burden_is_briefly_gone_covers_what_was_raised(store, alarm, tmp_path, path):
    alarm.tick(start=path == "completion")
    [request] = rows(store)
    alarm.kinds = set()
    if path == "completion":
        orch_role_raise.complete_call(store, request["id"], tmp_path)
    else:
        orch_role_raise.answer(store, request["id"], "not-now", "seen",
                               orch_role_raise.measure(store, tmp_path, request["id"]))
    alarm.kinds = {KIND}
    calls = len(alarm.calls)
    alarm.tick()
    alarm.tick()
    assert len(alarm.calls) == calls, "The same burden returning is not worse than what the answer covered"
    alarm.numbers[KIND]["lane_runs"] = 17
    alarm.tick()
    assert len(alarm.calls) == calls + 1


def test_an_unnamed_oldest_anchors_on_every_item_listed_at_the_answer():
    def facts(numbers):
        return orch_role_raise.burden({"kinds": {"waiting_on_person": {"numbers": numbers}}})

    before = facts({"waiting": 2, "oldest": None, "items": ["#7", "#8"], "oldest_minutes": None})
    worse = orch_role_raise.worse_facts
    assert not worse(before, facts({"waiting": 2, "oldest": "#7", "items": ["#7", "#8"]})), (
        "A marker read later names an oldest among the same waiters")
    assert not worse(before, facts({"waiting": 2, "oldest": "#9", "items": ["#8", "#9"]})), (
        "Someone listed at the answer still waits")
    assert worse(before, facts({"waiting": 2, "oldest": None, "items": ["#9", "#10"]})), "All of them left"
    assert worse(before, facts({"waiting": 3, "oldest": "#7", "items": ["#7", "#8", "#9"]}))
    named = facts({"waiting": 1, "oldest": "#7", "items": ["#7"], "oldest_minutes": 90})
    assert worse(named, facts({"waiting": 1, "oldest": None, "items": ["#9"]})), (
        "The named oldest left even though no start is known now")


@pytest.mark.parametrize("label", orch_facts_kinds.PERSON_LABELS)
@pytest.mark.parametrize("path", ["completion", "signal-answer"])
def test_a_marker_read_after_the_answer_never_calls_again(store, alarm, tmp_path, label, path):
    """Review round 5: oldest goes from unknown to #7 while the sole waiter is unchanged."""
    store.record_issue(Issue(7, "Needs a decision", "", frozenset({label}), "open", False), {})
    alarm.kinds = {"waiting_on_person"}

    def measure():
        alarm.numbers = {"waiting_on_person": orch_facts_kinds.waiting_on_person_numbers(store, store.clock[0])}
        return alarm.numbers["waiting_on_person"]

    assert measure()["oldest"] is None
    alarm.tick(minutes=0, start=path == "completion")
    [request] = rows(store)
    if path == "completion":
        orch_role_raise.complete_call(store, request["id"], tmp_path)
    else:
        orch_role_raise.answer(store, request["id"], "not-now", "seen",
                               orch_role_raise.measure(store, tmp_path, request["id"]))
    store.record_comments(7, [{"id": 1, "body": MARKERS[label], "created_at": NOW.isoformat()}])
    store.clock[0] += timedelta(hours=2)
    assert measure()["oldest"] == "#7"
    calls = len(alarm.calls)
    alarm.tick()
    alarm.tick(start=False)
    assert len(alarm.calls) == calls, "The same sole waiter must stay quiet"
    assert len(rows(store)) == 1, "A no-start tick must not raise it again either"
    store.record_issue(Issue(7, "Needs a decision", "", frozenset(), "open", False), {})
    store.record_issue(Issue(9, "Another", "", frozenset({label}), "open", False), {})
    measure()
    alarm.tick()
    assert len(alarm.calls) == calls + 1, "The waiter at the answer left and someone else waits"
