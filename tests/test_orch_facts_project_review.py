"""Spend and scope drift from the frozen read: machine-wide basis, unknowns and live workloads."""

from datetime import UTC, datetime, timedelta
import os
import subprocess
import sys

import pytest

from foragent import capacity, capacity_sessions, orch_events, orch_facts
from foragent.config import Config
from foragent.state import StateStore


NOW = datetime(2026, 10, 30, 12, tzinfo=UTC)
# Fake process table: these pids are never signalled.
LIVE, OTHER, DEAD = 910001, 910003, 910002


@pytest.fixture
def machine(tmp_path, monkeypatch):
    root = tmp_path / "machine"
    monkeypatch.setattr(capacity, "machine_root", lambda: root)
    monkeypatch.setattr(capacity, "machine_load", lambda: ("normal", 0.5))
    monkeypatch.setattr(orch_facts, "_probe", lambda pid: pid != DEAD)
    return root


def make_store(root, name="project"):
    clock = [NOW]
    store = StateStore(root / name, now=lambda: clock[0])
    store.clock = clock
    return store


def record(store, number, seconds, rounds=None, *, lane="orch", label="size:m", declared=None, changed=None,
           ago=timedelta(hours=2), outcome="merged", issue=True):
    clock = store.clock
    clock[0] = NOW - ago - timedelta(seconds=seconds)
    store.start_run(number, branch=f"issue-{number}", worktree=store.worktree_path(number), lane=lane, tier="s")
    store.update_run(number, declared_files=declared, changed_files=changed)
    if issue:
        store.update_local_issue(number, title=f"Task {number}", body="", labels=[label], state="closed")
    for round_number in range(1, (rounds or 0) + 1):
        at = clock[0] + timedelta(seconds=round_number)
        path = store.root / "runs" / f"issue-{number}" / f"review-{round_number}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("VERDICT: PASS\n")
        os.utime(path, (at.timestamp(), at.timestamp()))
        orch_events.append_event(store, at, number, "review", "PASS 0 findings", verdict="PASS",
                                 findings=0, round=round_number, source="reviewer")
    clock[0] += timedelta(seconds=seconds)
    store.finish_run(number, outcome)
    clock[0] = NOW


def collect(store, tmp_path):
    return orch_facts.collect(store, Config(repo="owner/project", skill_libraries=[], language="en"), tmp_path)


def row_of(facts, number):
    return next(row for row in facts["runs"] if row["issue"] == number)


def test_basis_is_machine_wide_and_rounds_share_its_population(machine, tmp_path):
    store, sibling = make_store(machine), make_store(machine, "sibling")
    record(store, 1, 600, 1)
    record(sibling, 2, 900, 2)
    record(sibling, 3, 1200, 3)
    record(sibling, 4, 1800, 4)
    spend = row_of(collect(store, tmp_path), 1)["spend"]
    assert spend["typical_seconds"] == 1050 and spend["basis_runs"] == 4 and spend["source"] == "merged_runs"
    assert spend["typical_rounds"] == 2.5 and spend["rounds_basis_runs"] == 4
    assert spend["as_of"] == NOW.isoformat()


def test_basis_window_and_size_keep_other_samples_out(machine, tmp_path):
    store = make_store(machine)
    record(store, 1, 100, 9, ago=timedelta(days=15))  # Outside the ETA's 14 days.
    record(store, 2, 200, 9, label="size:s")  # Another size.
    record(store, 3, 300, 1, outcome="checks-failed")  # Not a merge.
    record(store, 4, 400, 1)
    spend = row_of(collect(store, tmp_path), 4)["spend"]
    assert spend["typical_seconds"] == 5400 and spend["basis_runs"] is None and spend["source"] == "size_default"
    assert spend["typical_rounds"] is None and spend["rounds_ratio"] is None
    assert row_of(collect(store, tmp_path), 2)["spend"]["size"] == "s"


def test_unknown_rounds_leave_the_median_and_can_drop_below_the_minimum(machine, tmp_path):
    store = make_store(machine)
    record(store, 1, 600, 1)
    record(store, 2, 900, 2)
    record(store, 3, 1200)  # A merged run with no reviewer record: unknown, not zero.
    record(store, 4, 1800, 3)
    result = collect(store, tmp_path)
    assert row_of(result, 3)["spend"]["actual_rounds"] is None and row_of(result, 3)["spend"]["rounds_ratio"] is None
    # Three known rounds still meet the minimum; the unknown one is not a zero in the median.
    assert row_of(result, 4)["spend"]["typical_rounds"] == 2
    assert row_of(result, 4)["spend"]["basis_runs"] == 4 and row_of(result, 4)["spend"]["rounds_basis_runs"] == 3


def test_two_known_rounds_are_below_the_minimum(machine, tmp_path):
    store = make_store(machine)
    record(store, 1, 600, 1)
    record(store, 2, 900, 2)
    record(store, 3, 1200)
    spend = row_of(collect(store, tmp_path), 1)["spend"]
    assert spend["basis_runs"] == 3 and spend["typical_rounds"] is None and spend["rounds_basis_runs"] is None


def test_issue_without_a_local_record_has_no_known_size(machine, tmp_path):
    store = make_store(machine)
    record(store, 1, 600, 1, issue=False)
    spend = row_of(collect(store, tmp_path), 1)["spend"]
    assert spend["size"] is None and spend["typical_seconds"] is None and spend["runtime_ratio"] is None
    assert spend["source"] is None


@pytest.mark.parametrize("declared,changed,outside", [
    (None, ["a.py"], None), (["a.py"], None, None), ([], [], []), ([], ["a.py"], ["a.py"]),
    (["src/**"], ["src/deep/a.py", "b.py", "b.py"], ["b.py"]),
    (["src/*.py"], ["src/deep/a.py"], ["src/deep/a.py"]),
    (["a[1].py"], ["a1.py", "a[1].py"], ["a[1].py"]),
])
def test_scope_drift_uses_captured_sets_and_gate_globs(machine, tmp_path, declared, changed, outside):
    store = make_store(machine)
    record(store, 1, 600, 1, declared=declared, changed=changed)
    drift = row_of(collect(store, tmp_path), 1)["scope_drift"]
    assert drift["outside_declared"] == outside
    assert drift["declared"] == declared
    assert drift["changed"] == (None if changed is None else list(dict.fromkeys(changed)))


def test_area_sums_paired_ratios_unknown_counts_and_every_execution(machine, tmp_path):
    store = make_store(machine)
    record(store, 1, 600, 2, declared=["a.py"], changed=["a.py"], ago=timedelta(hours=9))
    record(store, 1, 1200, None, declared=["a.py"], changed=["b.py"], ago=timedelta(hours=5))
    record(store, 2, 300, 1, ago=timedelta(hours=3))
    area = collect(store, tmp_path)["areas"]["orch"]
    assert area["executions"] == 3
    spend = area["spend"]
    assert spend["actual_seconds"] == 2100 and spend["actual_rounds"] == 3
    assert spend["unknown"]["actual_rounds"] == 1 and spend["unknown"]["actual_seconds"] == 0
    assert spend["sources"] == {"size_default": 3} and spend["typical_rounds"] is None
    assert spend["runtime_ratio"] == pytest.approx(2100 / (3 * 5400)) and spend["rounds_ratio"] is None
    assert [run["started_at"] for run in area["runs"]] == sorted(run["started_at"] for run in area["runs"])
    assert [run["scope_drift"]["outside_declared"] for run in area["runs"]] == [[], ["b.py"], None]
    assert area["scope_drift"]["1"]["outside_declared"] == ["b.py"], "The issue's latest execution"
    assert area["scope_drift_unknown"] == 1


def test_human_lines_are_localized_and_keep_unknown_visible(machine, tmp_path):
    store = make_store(machine)
    record(store, 1, 600, None, declared=[], changed=["x.py"])
    result = collect(store, tmp_path)
    english, korean = orch_facts.human(result, "en"), orch_facts.human(result, "ko")
    assert "Spend" in english and "Scope drift" in english and "#1 x.py" in english
    assert "소비" in korean and "범위 이탈" in korean and "#1 x.py" in korean and "Scope drift" not in korean
    assert "라운드 모름" in korean and len(english.split("\n\n")) == 5


def add_workload(store, pid, kind="session", issue=None):
    capacity_sessions.add(store, capacity_sessions.new_path(store, pid), pid=pid, kind=kind, issue=issue)


def machine_runs(store, tmp_path):
    return collect(store, tmp_path)["resources"]["parallelizable_work"]["machine_runs"]


def test_capacity_counts_live_workloads_including_workload_only_sibling(machine, tmp_path):
    store, sibling = make_store(machine), make_store(machine, "sibling")
    store.root.mkdir(parents=True)
    assert machine_runs(store, tmp_path) == 0
    add_workload(sibling, LIVE)
    assert machine_runs(store, tmp_path) == 1, "A store with only a live workload still holds a slot"
    add_workload(store, OTHER)
    assert machine_runs(store, tmp_path) == 2
    add_workload(store, DEAD)
    add_workload(sibling, DEAD)
    assert machine_runs(store, tmp_path) == 2, "A dead process stops counting while its record remains"


def test_capacity_does_not_charge_a_run_twice_for_its_first_workload(machine, tmp_path):
    store = make_store(machine)
    store.start_run(1, branch="issue-1", worktree=store.worktree_path(1), pid=LIVE)
    add_workload(store, LIVE, kind="review", issue=1)
    assert machine_runs(store, tmp_path) == 1
    add_workload(store, LIVE, kind="review", issue=1)
    assert machine_runs(store, tmp_path) == 2, "Each further reviewer is its own agent"


def test_waiting_parent_holds_nothing_but_its_workload_does(machine, tmp_path):
    store = make_store(machine)
    store.start_run(1, branch="issue-1", worktree=store.worktree_path(1), pid=LIVE)
    store.update_run(1, phase="ci")
    assert machine_runs(store, tmp_path) == 0
    add_workload(store, LIVE, kind="review", issue=1)
    assert machine_runs(store, tmp_path) == 1


def test_fresh_process_reaches_the_case_rule_without_an_import_cycle(tmp_path):
    """The real command imports facts first, unlike a test session that already imported the scheduler."""
    code = ("import pathlib, sys; from foragent import orch_facts; from foragent.config import Config; "
            "from foragent.state import StateStore; root = pathlib.Path(sys.argv[1]); "
            "store = StateStore(root / 'store'); store.root.mkdir(); "
            "print(orch_facts.collect(store, Config(repo='o/p', skill_libraries=[]), root)['areas'])")
    done = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True, check=False,
                          env={**os.environ, "HOME": str(tmp_path)})
    assert done.returncode == 0 and done.stdout.strip() == "{}", done.stderr


def test_a_recorded_unusable_round_is_zero_usable_rounds_not_unknown(machine, tmp_path):
    store = make_store(machine)
    record(store, 1, 600)
    at = NOW - timedelta(hours=2, seconds=300)
    orch_events.append_event(store, at, 1, "review", "UNUSABLE", verdict="UNUSABLE", findings=0, round=1, source="reviewer")
    assert row_of(collect(store, tmp_path), 1)["spend"]["actual_rounds"] == 0


def test_basis_keeps_the_last_thirty_merges_of_a_size(machine, tmp_path):
    store = make_store(machine)
    record(store, 100, 90000, ago=timedelta(days=5))  # The oldest sample leaves the basis.
    for number in range(1, 31):
        record(store, number, 600, ago=timedelta(hours=number))
    spend = row_of(collect(store, tmp_path), 1)["spend"]
    assert spend["basis_runs"] == 30 and spend["typical_seconds"] == 600


def test_area_ratio_leaves_out_an_execution_with_an_unknown_side(machine, tmp_path):
    store = make_store(machine)
    record(store, 1, 600, 1)
    record(store, 2, 900, 2)
    record(store, 3, 1200, 3)
    record(store, 4, 1800)  # Unknown rounds: its typical rounds are not in the ratio's basis.
    spend = collect(store, tmp_path)["areas"]["orch"]["spend"]
    assert spend["actual_rounds"] == 6 and spend["typical_rounds"] == 8 and spend["rounds_ratio"] == 1
    assert spend["unknown"]["actual_rounds"] == 1


def test_project_review_writes_nothing(machine, tmp_path):
    store, sibling = make_store(machine), make_store(machine, "sibling")
    record(store, 1, 600, 1)
    record(sibling, 2, 900, 2)
    add_workload(sibling, DEAD)
    before = {path: path.read_bytes() for path in machine.rglob("*") if path.is_file()}
    assert collect(store, tmp_path) == collect(store, tmp_path)
    assert {path: path.read_bytes() for path in machine.rglob("*") if path.is_file()} == before
