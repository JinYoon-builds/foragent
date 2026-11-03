"""Gate alarms that call the architect directly (#1469): pending retries, worse-only rearming
against one answered baseline, and finished-dive completion."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from foragent import orch_architect, orch_role_raise, orch_roles
from foragent.state import StateStore

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
    assert not worse(facts("merge_stall", {"queued": 2}), facts("merge_stall", {"queued": 3}))
    assert worse(facts("merge_stall", {"queued": 2}), facts("merge_stall", {"queued": 4})), "Doubled"
    idle = {"idle_lanes": ["a"], "ready_by_lane": {"b": 3}}
    assert not worse(facts("idle_lanes_with_backlog", idle, {"kind": "cap", "normal": False}),
                     facts("idle_lanes_with_backlog", idle, {"kind": "cap", "normal": True}))
    assert worse(facts("idle_lanes_with_backlog", idle, {"kind": "cap", "normal": True}),
                 facts("idle_lanes_with_backlog", idle, {"kind": "cap", "normal": False}))


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


@pytest.mark.parametrize("start", [False, True], ids=["no-start", "start"])
@pytest.mark.parametrize("path", ["completion", "signal-answer"])
def test_every_path_compares_with_the_numbers_at_the_answer(store, alarm, tmp_path, path, start):
    """Review round 6: 16/18 at the raise, 17/18 at the answer, then 17/18 again stays quiet."""
    alarm.numbers[KIND]["lane_runs"] = 16
    alarm.tick(start=path == "completion")
    [request] = rows(store)
    alarm.numbers[KIND]["lane_runs"] = 17
    if path == "completion":
        orch_role_raise.complete_call(store, request["id"], tmp_path)
    else:
        orch_role_raise.answer(store, request["id"], "not-now", "seen",
                               orch_role_raise.measure(store, tmp_path, request["id"]))
    calls = len(alarm.calls)
    for _ in range(2):
        alarm.tick(start=start)
    assert len(rows(store)) == 1 and len(alarm.calls) == calls, "Unchanged since the answer"
    alarm.numbers[KIND]["lane_runs"] = 18
    alarm.tick(start=start)
    assert len(rows(store)) == 2, "A share above the answer's is worse on every path"
    assert len(alarm.calls) == calls + (1 if start else 0)


@pytest.mark.parametrize("start", [False, True], ids=["no-start", "start"])
@pytest.mark.parametrize("held,expected", [(4, 0), (5, 0), (6, 1), (18, 1)])
def test_a_count_of_an_existing_member_must_at_least_double(
        store, alarm, tmp_path, monkeypatch, start, held, expected):
    """Holds 3 to 4 or 5 stay quiet; 3 to 6 or 18 is worse, on every path."""
    alarm.kinds = {"repeated_design_holds"}
    alarm.numbers = {"repeated_design_holds": {"holds_by_issue": {"#1004": 3}}}
    signal = {"paths": [], "kinds": ["repeated_design_holds"]}
    monkeypatch.setattr(orch_role_raise, "load_roles", lambda config: {"architect": {"signals": [signal]}})
    alarm.tick(start=False)
    [request] = rows(store)
    orch_role_raise.answer(store, request["id"], "not-now", "seen", orch_role_raise.measure(store, tmp_path, request["id"]))
    alarm.numbers = {"repeated_design_holds": {"holds_by_issue": {"#1004": held}}}
    alarm.tick(start=start)
    assert len(rows(store)) == 1 + expected
    assert len(alarm.calls) == (expected if start else 0)


@pytest.mark.parametrize("start", [False, True], ids=["no-start", "start"])
@pytest.mark.parametrize("kind,field,extra", [
    ("idle_lanes_with_backlog", "ready_by_lane", {"idle_lanes": ["order"]}),
    ("hot_overlap_files", "runs_by_file", {}),
    ("repeated_design_holds", "holds_by_issue", {}),
    ("files_over_limit", "lines_by_file", {"limit": 500}),
], ids=["lane-named-share", "file-named-share", "issue-named-share", "lines-named-share"])
@pytest.mark.parametrize("member", ["share", "runs", "lane_runs"])
def test_a_member_named_like_a_share_or_context_field_is_still_a_member(
        store, alarm, tmp_path, monkeypatch, start, kind, field, extra, member):
    """Review round 7: only a kind's declared top-level share rises; members must double."""
    signal = {"paths": [], "kinds": [kind]}
    monkeypatch.setattr(orch_role_raise, "load_roles", lambda config: {"architect": {"signals": [signal]}})
    alarm.kinds = {kind}
    alarm.numbers = {kind: {**extra, field: {member: 4}}}
    alarm.tick(start=False)
    [request] = rows(store)
    orch_role_raise.answer(store, request["id"], "not-now", "seen", orch_role_raise.measure(store, tmp_path, request["id"]))
    alarm.numbers = {kind: {**extra, field: {member: 7}}}
    alarm.tick(start=start)
    assert len(rows(store)) == 1 and alarm.calls == [], f"{field}[{member!r}] 4 to 7 has not doubled"
    alarm.numbers = {kind: {**extra, field: {member: 8}}}
    alarm.tick(start=start)
    assert len(rows(store)) == 2 and len(alarm.calls) == (1 if start else 0), "Doubled"


def test_the_declared_share_is_compared_only_at_its_kind_level():
    def facts(numbers):
        return {"kinds": {"lane_concentration": {"numbers": numbers}}}

    worse = orch_role_raise.worse
    base = {"runs": 18, "lane": "roles", "lane_runs": 16, "idle_lanes": ["order", "view"]}
    assert worse(facts(base), facts({**base, "lane_runs": 17, "runs": 19})), "The share rose"
    assert not worse(facts(base), facts({**base, "runs": 19})), "The share fell"
