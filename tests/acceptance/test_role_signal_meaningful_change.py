"""Locked acceptance checks for signal-meaningful-change; no implementation here.

Only the measured fa orch facts input is faked. Raising uses fa gate tick;
answering and reading use fa orch signal-answer and fa orch inbox --json.
The existing offline fixture supplies tracker, process and clock boundaries.
Each tick reopens the store, so persistence is exercised without seeding any
fingerprint or suppression record.

Break-it: comparing raw counts, using only a timer, permanently muting a kind,
ignoring new affected entities, or raising twice must fail these checks.
"""

from copy import deepcopy
from datetime import timedelta

import pytest

from test_role_source import DEFAULTS, role_entry, write_role_snapshot
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401
from test_tick_failure_alert import inbox, run_tick, tick_case  # noqa: F401


CASES = [
    pytest.param(
        "hot_overlap_files",
        {"runs_by_file": {"skills/fa-order/SKILL.md": 18, "docs/decisions.md": 8}},
        {"runs_by_file": {"docs/decisions.md": 7, "skills/fa-order/SKILL.md": 17}},
        {"runs_by_file": {"docs/decisions.md": 7, "skills/fa-order/SKILL.md": 17,
                          "src/foragent/orch_inbox.py": 3}},
        id="hot-files",
    ),
    pytest.param(
        "idle_lanes_with_backlog",
        {"idle_lanes": ["order", "view"], "ready_by_lane": {"roles": 4}},
        {"idle_lanes": ["view", "order"], "ready_by_lane": {"roles": 5}},
        {"idle_lanes": ["view", "order", "skills"], "ready_by_lane": {"roles": 5}},
        id="idle-lanes",
    ),
    pytest.param(
        "files_over_limit",
        {"limit": 500, "lines_by_file": {"src/large.py": 550}},
        {"limit": 500, "lines_by_file": {"src/large.py": 560}},
        {"limit": 500, "lines_by_file": {"src/large.py": 560, "src/new.py": 501}},
        id="oversized-files",
    ),
    pytest.param(
        "repeated_design_holds",
        {"holds_by_issue": {"#41": 3}},
        {"holds_by_issue": {"#41": 4}},
        {"holds_by_issue": {"#41": 4, "#42": 2}},
        id="repeated-holds",
    ),
]


def signals(case, capsys):
    run_tick(case, capsys)
    rows = [row for row in inbox(case, capsys) if row.get("role") == "architect"]
    assert case.pushes == [], "Role signals belong in the orchestrator inbox"
    return rows


def answer(case, capsys, request_id, how):
    capsys.readouterr()
    code = cli_exit(case.main, [
        "orch", "signal-answer", request_id, how, "--why",
        "The architect already considered these affected entities and the current constraint",
        "--repo-root", str(case.root),
    ])
    output = capsys.readouterr()
    assert code == 0, f"The existing signal-answer command must work: {output.err}"
    assert next(row for row in inbox(case, capsys) if row["id"] == request_id)["status"] == "done"


@pytest.mark.parametrize("kind,initial,drift,new_evidence", CASES)
@pytest.mark.parametrize("how", ["not-now", "called"])
def test_count_drift_stays_quiet_and_a_new_affected_entity_raises_once(
        tick_case, monkeypatch, capsys, kind, initial, drift, new_evidence, how):  # noqa: F811
    from foragent import orch_facts

    case = tick_case
    roles = {name: role_entry(name) for name in DEFAULTS}
    signal = {"paths": [], "kinds": [kind]}
    roles["architect"]["signals"] = [signal]
    write_role_snapshot(case.root, roles)
    snapshot = {
        "kinds": [kind], "kind_numbers": {kind: deepcopy(initial)},
        "binding": {"kind": "cap", "normal": False, "detail": "No free slot for #41"},
    }

    def measured_facts(*args, **kwargs):
        facts = deepcopy(snapshot)
        facts["at"] = case.clock[0].isoformat()
        return facts

    monkeypatch.setattr(orch_facts, "collect", measured_facts)
    first_rows = signals(case, capsys)
    assert len(first_rows) == 1, "The first met signal must raise exactly one request"
    first = first_rows[0]
    assert first["from"] == "gate" and first["signal"] == signal and first["status"] != "done"
    answer(case, capsys, first["id"], how)

    snapshot["kind_numbers"][kind] = deepcopy(drift)
    # Exercise both sides of the old 12-hour timer, plus another gate restart.
    for advance in (timedelta(), timedelta(hours=13), timedelta(days=2)):
        case.clock[0] += advance
        current = signals(case, capsys)
        assert [row["id"] for row in current] == [first["id"]], (
            f"{kind}: {how} must stay quiet when only counts/order within the same affected set change"
        )
        assert current[0]["status"] == "done"

    snapshot["kind_numbers"][kind] = deepcopy(new_evidence)
    current = signals(case, capsys)
    added = [row for row in current if row["id"] != first["id"]]
    assert len(current) == 2 and len(added) == 1, (
        f"{kind}: a newly affected file, issue, or idle lane must raise exactly one fresh request"
    )
    second = added[0]
    assert second["signal"] == signal and second["status"] != "done"
    assert next(row for row in current if row["id"] == first["id"])["status"] == "done"
    expected = {first["id"], second["id"]}
    for _ in range(3):
        assert {row["id"] for row in signals(case, capsys)} == expected, (
            "An open request must not be duplicated on the next ticks"
        )
    answer(case, capsys, second["id"], how)
    case.clock[0] += timedelta(hours=13)
    final = signals(case, capsys)
    assert {row["id"] for row in final} == expected, "Newly answered evidence must stay quiet too"
    assert all(row["status"] == "done" for row in final)
