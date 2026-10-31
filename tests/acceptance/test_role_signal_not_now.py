"""Acceptance test for signal-not-now-sticks. Locked by Foragent; do not edit.

Break-it checks (all must fail on main before the feature exists):
1. Same facts: rejects treating not-now as a timer or hashing the observation time.
2. Changed facts: rejects permanent suppression, a cooldown-only fix, or comparing
   only whether the signal is met; each fact change must re-arm it exactly once.
3. Different signal: rejects muting an entire role or comparing unrelated facts.
4. Reopened store: rejects keeping suppression only on the old StateStore object.

The existing offline gate fixture supplies the tracker, clock and process fakes.
Only the measured facts input is replaced; gate raising, answers and inbox reads
run through the public CLI. No suppression records are seeded or inspected.
"""

from copy import deepcopy
from datetime import timedelta

import pytest

from test_role_source import DEFAULTS, role_entry, write_role_snapshot
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401 (fixture)
from test_tick_failure_alert import inbox, run_tick, tick_case  # noqa: F401 (fixture)


IDLE = "idle_lanes_with_backlog"
OTHER = "hot_overlap_files"
CAP = {"kind": "cap", "normal": False, "detail": "no free parallel slot for #41"}
IDLE_SIGNAL = {"paths": [], "kinds": [IDLE]}
OTHER_SIGNAL = {"paths": [], "kinds": [OTHER]}


@pytest.fixture
def not_now_case(tick_case, monkeypatch):  # noqa: F811
    from foragent import orch_facts

    case = tick_case
    roles = {name: role_entry(name) for name in DEFAULTS}
    roles["architect"]["signals"] = [IDLE_SIGNAL, OTHER_SIGNAL]
    write_role_snapshot(case.root, roles)
    case.facts = {
        "kinds": [IDLE],
        "kind_numbers": {
            IDLE: {"idle_lanes": ["order", "view"], "ready_by_lane": {"orch": 4}},
        },
        "binding": deepcopy(CAP),
    }

    def measured_facts(*args, **kwargs):
        # Time advances on every real CLI tick. Observation time is not a change
        # to the binding constraint, idle lanes or their ready work.
        snapshot = deepcopy(case.facts)
        snapshot["at"] = case.clock[0].isoformat()
        return snapshot

    monkeypatch.setattr(orch_facts, "collect", measured_facts)
    return case


def tick_signals(case, capsys):
    run_tick(case, capsys)
    rows = [row for row in inbox(case, capsys) if row.get("role") == "architect"]
    assert case.pushes == [], "Role signals stay in the orchestrator inbox"
    return rows


def defer(case, capsys, request_id):
    capsys.readouterr()
    code = cli_exit(case.main, [
        "orch", "signal-answer", request_id, "not-now", "--why",
        "The current constraint and ready work have already been considered",
        "--repo-root", str(case.root),
    ])
    output = capsys.readouterr()
    assert code == 0, f"fa orch signal-answer must succeed: {output.err}"
    rows = {row["id"]: row for row in inbox(case, capsys)}
    assert rows[request_id]["status"] == "done", "not-now resolves the public request"


def first_not_now(case, capsys):
    rows = tick_signals(case, capsys)
    assert len(rows) == 1 and rows[0]["signal"] == IDLE_SIGNAL, (
        "The first met signal must raise one request through fa gate tick"
    )
    assert rows[0]["from"] == "gate" and rows[0]["status"] != "done"
    first = rows[0]["id"]
    defer(case, capsys, first)
    return first


def test_same_facts_produce_zero_reraises_across_ticks(not_now_case, capsys):
    """Rejects timer expiry and observation timestamp churn as new evidence."""
    case = not_now_case
    first = first_not_now(case, capsys)
    for advance in (timedelta(), timedelta(), timedelta(hours=13), timedelta(days=2)):
        case.clock[0] += advance
        rows = tick_signals(case, capsys)
        assert [row["id"] for row in rows] == [first], (
            "Same facts must produce 0 re-raises, even after the old cooldown expires"
        )
        assert rows[0]["status"] == "done"


@pytest.mark.parametrize("binding,idle_lanes,ready_by_lane", [
    ({"kind": "quota", "normal": True, "detail": "usage-limit pause holds #41"},
     ["order", "view"], {"orch": 4}),
    (CAP, ["order", "skills", "view"], {"orch": 4}),
    (CAP, ["order", "view"], {"orch": 5}),
], ids=["binding-changes", "idle-lanes-change", "ready-work-changes"])
def test_changed_facts_raise_exactly_one_new_request(
        not_now_case, capsys, binding, idle_lanes, ready_by_lane):
    """Rejects a permanent mute, a cooldown, and met/unmet-only comparisons."""
    case = not_now_case
    first = first_not_now(case, capsys)
    case.facts["binding"] = deepcopy(binding)
    case.facts["kind_numbers"][IDLE] = {
        "idle_lanes": list(idle_lanes), "ready_by_lane": dict(ready_by_lane),
    }
    rows = tick_signals(case, capsys)  # Five minutes later, still continuously met.
    added = [row for row in rows if row["id"] != first]
    assert len(added) == 1, "Changed facts must raise exactly 1 new request on the next tick"
    new = added[0]
    assert new["signal"] == IDLE_SIGNAL and new["status"] != "done"
    assert next(row for row in rows if row["id"] == first)["status"] == "done"
    for _ in range(3):
        assert {row["id"] for row in tick_signals(case, capsys)} == {first, new["id"]}, (
            "Changed facts must not create duplicate open requests"
        )
    defer(case, capsys, new["id"])
    case.clock[0] += timedelta(hours=13)
    assert {row["id"] for row in tick_signals(case, capsys)} == {first, new["id"]}, (
        "A second not-now must suppress its newly considered facts too"
    )


def test_a_different_signal_is_unaffected_on_the_same_role(not_now_case, capsys):
    """Rejects a role-wide mute and treating another signal's facts as this one's."""
    case = not_now_case
    first = first_not_now(case, capsys)
    case.facts["kinds"].append(OTHER)
    case.facts["kind_numbers"][OTHER] = {"runs_by_file": {"src/shared.py": 3}}
    case.clock[0] += timedelta(hours=13)
    rows = tick_signals(case, capsys)
    added = [row for row in rows if row["id"] != first]
    assert len(added) == 1 and added[0]["signal"] == OTHER_SIGNAL, (
        "Only the different signal may raise; the unchanged not-now signal stays quiet"
    )
    assert added[0]["status"] != "done"
    assert next(row for row in rows if row["id"] == first)["status"] == "done"
    expected = {first, added[0]["id"]}
    for _ in range(3):
        assert {row["id"] for row in tick_signals(case, capsys)} == expected


def test_suppression_survives_a_gate_restart_with_store_reopened(not_now_case, capsys):
    """Rejects suppression attached only to the previous store instance."""
    from foragent.state import StateStore

    case = not_now_case
    first = first_not_now(case, capsys)
    old_store = case.store
    case.store = StateStore(old_store.root, now=lambda: case.clock[0])
    assert case.store is not old_store and case.store.root == old_store.root
    case.clock[0] += timedelta(hours=13)
    for _ in range(3):
        rows = tick_signals(case, capsys)
        assert case.store is not old_store, "The restarted gate must use a reopened store"
        assert [row["id"] for row in rows] == [first], (
            "Reopened state must preserve not-now suppression for unchanged facts"
        )
        assert rows[0]["status"] == "done"
