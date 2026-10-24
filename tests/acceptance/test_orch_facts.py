"""Acceptance test for orch-facts. Locked by Foragent; do not edit."""

from datetime import UTC, datetime, timedelta
import json
import os
import re
import subprocess

import pytest

from test_local_truth import record_gh
from test_order_progress import offline_local_cli
from test_skills_autosync import cli_exit, isolated_home


NOW = datetime(2026, 10, 24, 12, tzinfo=UTC)
PHASE_FIELDS = ("implementing_seconds", "checking_seconds", "test_slot_wait_seconds", "baseline_seconds")
SECTIONS = {"runs", "test_slots", "hot_files", "failure_reasons", "merges_since_tidy"}


@pytest.fixture(autouse=True)
def no_facts_processes(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("fa orch facts must only read local records, never launch or signal a process")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "kill", forbidden)
    monkeypatch.setattr(os, "killpg", forbidden)


def facts_case(tmp_path, monkeypatch):
    from foragent import cli
    from foragent.state import StateStore

    case = offline_local_cli(tmp_path, monkeypatch)
    (case.root / ".foragent.yml").write_text(
        "repo: owner/repo\nlanguage: en\nskill_libraries: []\nnotify: none\ntest_slots: 4\n")
    case.clock = [NOW]
    case.store = StateStore(case.store.root, now=lambda: case.clock[0])
    monkeypatch.setattr(StateStore, "for_repo", classmethod(
        lambda cls, repo, **kw: cls(case.store.root, now=lambda: case.clock[0])))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return case.clock[0] if tz else case.clock[0].replace(tzinfo=None)

    monkeypatch.setattr(cli, "datetime", Clock)
    # Use the shared real-gh guard, rather than hiding network reads in a tracker fake.
    case.gh_calls = record_gh(monkeypatch)
    case.files_root = tmp_path
    return case


def files(root):
    return {str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in root.rglob("*") if path.is_file()}


def facts(case, capsys, *, human=False):
    capsys.readouterr()
    before = files(case.files_root)
    argv = ["orch", "facts", "--repo-root", str(case.root)] + ([] if human else ["--json"])
    code = cli_exit(case.main, argv)
    output = capsys.readouterr()
    assert files(case.files_root) == before, "Observing facts changed local files (including event/heartbeat files)"
    assert case.gh_calls == [], "Facts must never call gh"
    assert code == 0, f"fa orch facts must exist and succeed: {output.err.strip()}"
    if human:
        return output.out
    result = json.loads(output.out)
    assert SECTIONS <= result.keys(), f"Missing dashboard sections: {SECTIONS - result.keys()}"
    assert isinstance(result["runs"], list)
    assert isinstance(result["test_slots"], dict)
    assert isinstance(result["hot_files"], dict)
    assert isinstance(result["failure_reasons"], dict)
    assert type(result["merges_since_tidy"]) is int and result["merges_since_tidy"] >= 0
    for row in result["runs"]:
        assert {"issue", "waited_seconds", *PHASE_FIELDS} <= row.keys()
        assert type(row["issue"]) is int
        assert type(row["waited_seconds"]) in (int, float) and row["waited_seconds"] >= 0
        assert all(row[key] is None or (type(row[key]) in (int, float) and row[key] >= 0)
                   for key in PHASE_FIELDS)
    slots = result["test_slots"]
    assert {"current", "queue_length", "total_wait_seconds"} <= slots.keys()
    assert type(slots["current"]) is int and slots["current"] >= 1
    assert type(slots["queue_length"]) is int and slots["queue_length"] >= 0
    assert type(slots["total_wait_seconds"]) in (int, float) and slots["total_wait_seconds"] >= 0
    for section in ("hot_files", "failure_reasons"):
        assert all(isinstance(key, str) and type(count) is int and count > 0
                   for key, count in result[section].items())
    return result


def completed(case, number, *, started, phases=(), waited=0, outcome="merged", detail=""):
    """Seed Run records and the kind=phase event schema specified by #224.

    Explicit journal fixtures keep the facts consumer independent of whether the
    separate phase producer issue has shipped. Events close at cumulative times.
    """
    case.clock[0] = started
    case.store.start_run(number, branch=f"foragent/issue-{number}", worktree=case.store.worktree_path(number))
    events = []
    for phase, seconds in phases:
        case.clock[0] += timedelta(seconds=seconds)
        events.append({"at": case.clock[0].isoformat(), "issue": number, "kind": "phase",
                       "phase": phase, "seconds": seconds, "load": 2.5})
    if events:
        with (case.store.root / "orch-events.jsonl").open("a", encoding="utf-8") as stream:
            stream.writelines(json.dumps(event) + "\n" for event in events)
    case.store.update_run(number, waited_seconds=waited, last_progress_at=case.clock[0].isoformat())
    case.store.finish_run(number, outcome=outcome, detail=detail)
    case.clock[0] = NOW


def approved_issue(case, number, paths, *, labels=("approved", "ready"), state="open", active=False):
    from foragent.issue import body_hash, parse_body

    body = ("<!-- foragent:v1 -->\n## Goal\nChange the module.\n## Files\n"
            + "".join(f"- {path}\n" for path in paths)
            + "## Out of scope\nOther modules.\n## Done when\n```sh\ntrue\n```\n")
    assert parse_body(body).files == list(paths), "Use the same Files parser as gate's overlap check"
    case.store.save_approval(number, body=body, body_sha=body_hash(body), acceptance_blobs={},
                             approved_by="human", approved_at=NOW.isoformat())
    # The mutable snapshot deliberately disagrees: overlap uses the approved Files.
    case.store.update_local_issue(number, title=f"Issue {number}", labels=list(labels), state=state,
                                  body="## Files\n- src/unapproved-edit.py\n")
    if active:
        case.store.start_run(number, branch=f"foragent/issue-{number}", worktree=case.store.worktree_path(number))


def test_orch_facts_phase_seconds_belong_to_each_run_and_do_not_double_count_wait(tmp_path, monkeypatch, capsys):
    case = facts_case(tmp_path, monkeypatch)
    completed(case, 21, started=NOW - timedelta(hours=3), waited=20,
              phases=(("baseline", 3), ("implementing", 12), ("test-slot-wait", 17),
                      ("checking", 5), ("reviewing", 9)))
    completed(case, 21, started=NOW - timedelta(hours=2), waited=10,
              phases=(("implementing", 8), ("checking", 11), ("implementing", 7),
                      ("test-slot-wait", 6), ("baseline", 4)))
    completed(case, 22, started=NOW - timedelta(hours=1), waited=9)  # Legacy: no phase events.
    completed(case, 99, started=NOW - timedelta(days=40), phases=(("implementing", 9999),))
    result = facts(case, capsys)
    assert {row["issue"] for row in result["runs"]} == {21, 22}, "Only recent runs belong on the dashboard"
    measured = [row for row in result["runs"] if row["issue"] == 21]
    assert len(measured) == 2, "A retry must not replace an earlier execution of the same issue"
    assert sorted(tuple(row[key] for key in (*PHASE_FIELDS, "waited_seconds")) for row in measured) == [
        (12, 5, 17, 3, 20), (15, 11, 6, 4, 10),
    ], "Sum repeated phases within each execution; waited_seconds is a separate credited-wait total"
    legacy, = [row for row in result["runs"] if row["issue"] == 22]
    assert legacy["waited_seconds"] == 9
    assert all(legacy[key] in (None, 0) for key in PHASE_FIELDS), "Never guess phase allocation from total wait"


def test_orch_facts_test_slots_report_current_capacity_queue_and_only_test_wait(tmp_path, monkeypatch, capsys):
    from foragent import capacity

    case = facts_case(tmp_path, monkeypatch)
    capacity.update_test_slots(3, NOW, pressure="normal", load_per_cpu=0)
    completed(case, 1, started=NOW - timedelta(hours=2), waited=45,
              phases=(("test-slot-wait", 30), ("checking", 10)))
    completed(case, 2, started=NOW - timedelta(hours=1), waited=20,
              phases=(("test-slot-wait", 20), ("checking", 5)))
    for number in (3, 4, 5):
        case.store.start_run(number, branch=f"foragent/issue-{number}", worktree=case.store.worktree_path(number))
        case.store.update_run(number, phase="test-slot-wait" if number != 5 else "implementing",
                              phase_started_at=NOW.isoformat(),
                              waited_seconds=0 if number != 5 else 900,
                              waiting_until=(NOW + timedelta(minutes=1)).isoformat())
    slots = facts(case, capsys)["test_slots"]
    assert {"current", "queue_length", "total_wait_seconds"} <= slots.keys()
    assert (slots["current"], slots["queue_length"], slots["total_wait_seconds"]) == (3, 2, 50), (
        "Read the shared current slot count; count live slot waiters, and exclude executor/quota wait")


def test_orch_facts_hot_files_count_waiting_approved_issues_using_overlap_files(tmp_path, monkeypatch, capsys):
    case = facts_case(tmp_path, monkeypatch)
    approved_issue(case, 1, ["src/shared.py", "src/shared.py", "tests/test_runner.py", "src/plugins/**"])
    approved_issue(case, 2, ["src/shared.py", "tests/test_runner.py"])
    approved_issue(case, 3, ["src/shared.py"], labels=("approved",))
    approved_issue(case, 4, ["src/shared.py"], labels=("approved", "running"), active=True)
    approved_issue(case, 5, ["src/shared.py"], state="closed")
    approved_issue(case, 6, ["src/shared.py"], labels=("ready",))  # Stale approval file alone is insufficient.
    approved_issue(case, 7, [])  # Missing declarations must not be guessed from a diff or lane paths.
    hot = facts(case, capsys)["hot_files"]
    assert list(hot.items()) == [("src/shared.py", 3), ("tests/test_runner.py", 2), ("src/plugins/**", 1)], (
        "Count issues once per declared path, preserve globs, exclude active/closed/unapproved issues, sort descending")


def test_orch_facts_failure_reasons_group_detail_text_in_a_recent_window(tmp_path, monkeypatch, capsys):
    case = facts_case(tmp_path, monkeypatch)
    reason = "Acceptance baseline timed out"
    for number, outcome, detail in ((1, "checks-failed", reason), (2, "error", reason),
                                     (3, "checks-failed", "Unit test timed out"),
                                     (4, "merged", reason)):
        completed(case, number, started=NOW - timedelta(hours=2), outcome=outcome, detail=detail)
    completed(case, 1, started=NOW - timedelta(hours=1), outcome="checks-failed", detail=reason)
    completed(case, 98, started=NOW - timedelta(days=40), outcome="checks-failed", detail=reason)
    completed(case, 99, started=NOW + timedelta(days=2), outcome="checks-failed", detail=reason)
    assert facts(case, capsys)["failure_reasons"] == {reason: 3, "Unit test timed out": 1}, (
        "Group the recorded detail, not the outcome code; exclude merges, old records and future records")


def test_orch_facts_merges_since_latest_tidy_are_not_limited_to_recent_runs(tmp_path, monkeypatch, capsys):
    from foragent.orch_events import append_event

    case = facts_case(tmp_path, monkeypatch)
    completed(case, 1, started=NOW - timedelta(days=50))
    completed(case, 2, started=NOW - timedelta(days=40))
    assert facts(case, capsys)["merges_since_tidy"] == 2, "Without a tidy event, count all recorded merges"
    append_event(case.store, NOW - timedelta(days=45), None, "tidy-done", "Tidy completed")
    append_event(case.store, NOW - timedelta(days=30), None, "tidy-done", "Next tidy completed")
    completed(case, 3, started=NOW - timedelta(days=20))
    completed(case, 3, started=NOW - timedelta(hours=2))  # Two executions, not two copies of one record.
    completed(case, 4, started=NOW - timedelta(hours=1), outcome="blocked", detail="Waiting for a decision")
    assert facts(case, capsys)["merges_since_tidy"] == 2
    # A due request / architect call is not completion of tidy work.
    append_event(case.store, NOW - timedelta(minutes=10), None, "request", "tidy due: 30 merges")
    append_event(case.store, NOW - timedelta(minutes=5), None, "architect-call", "Inspect the hot files")
    assert facts(case, capsys)["merges_since_tidy"] == 2


def test_orch_facts_empty_and_human_views_are_short_local_and_read_only(tmp_path, monkeypatch, capsys):
    case = facts_case(tmp_path, monkeypatch)
    result = facts(case, capsys)
    assert result["runs"] == [] and result["hot_files"] == result["failure_reasons"] == {}
    assert result["merges_since_tidy"] == 0
    assert result["test_slots"] == {"current": 4, "queue_length": 0, "total_wait_seconds": 0}
    completed(case, 7, started=NOW - timedelta(hours=1), outcome="checks-failed", detail="Baseline timed out")
    approved_issue(case, 8, ["src/hot.py"])
    output = facts(case, capsys, human=True)
    blocks = re.split(r"\n\s*\n", output.strip())
    assert len(blocks) == 5, f"Show one short human block per section, got: {output}"
    for heading in (r"runs?", r"test[ _-]?slots?", r"hot[ _-]?files?", r"failure[ _-]?reasons?",
                    r"merges?.{0,30}tidy"):
        assert sum(bool(re.search(heading, block.splitlines()[0], re.I)) for block in blocks) == 1, heading
    assert "src/hot.py" in output and "Baseline timed out" in output and re.search(r"\b7\b", output)
    assert len(output.splitlines()) <= 30, "Keep the facts dashboard concise"
    assert facts(case, capsys) == facts(case, capsys), "Repeated observation must not add events or change facts"


def recorded_phase_run(case, number, *, started, phases, waited=0, outcome="merged", active_phase=None):
    """Phase instrumentation for D82 addendum 2, including explicit repeat links.

    Each (phase, seconds, metadata) occupies a disjoint interval. event_id and
    repeat_of identify repeated work without interpreting a log message. An open
    phase is measured from phase_started_at to the injected observation clock.
    """
    case.clock[0] = started
    case.store.start_run(number, branch=f"foragent/issue-{number}", worktree=case.store.worktree_path(number))
    with (case.store.root / "orch-events.jsonl").open("a", encoding="utf-8") as stream:
        for phase, seconds, metadata in phases:
            case.clock[0] += timedelta(seconds=seconds)
            stream.write(json.dumps({"kind": "phase", "issue": number, "phase": phase,
                                     "at": case.clock[0].isoformat(), "seconds": seconds,
                                     "load": 0.1, **metadata}) + "\n")
    case.store.update_run(number, waited_seconds=waited, last_progress_at=case.clock[0].isoformat())
    if active_phase is None:
        case.store.finish_run(number, outcome=outcome)
    else:
        case.store.update_run(number, phase=active_phase, phase_started_at=case.clock[0].isoformat(),
                              last_progress_at=NOW.isoformat())
    case.clock[0] = NOW


def test_orch_facts_history_medians_and_current_phase_comparison(tmp_path, monkeypatch, capsys):
    case = facts_case(tmp_path, monkeypatch)
    # Per-execution totals are 80, 200, 210, 170: median 185, whereas the
    # sum of the phase medians is 135. Aggregate phases within a run first.
    for number, hours, phases in (
        (11, 6, (("implementing", 50), ("checking", 10), ("baseline", 10), ("test-slot-wait", 10))),
        (12, 5, (("implementing", 60), ("checking", 12), ("checking", 8),
                 ("baseline", 20), ("test-slot-wait", 100))),
        (13, 4, (("implementing", 60), ("checking", 30), ("baseline", 100), ("test-slot-wait", 20))),
        (14, 3, (("implementing", 70), ("checking", 40), ("baseline", 30), ("test-slot-wait", 30))),
    ):
        completed(case, number, started=NOW - timedelta(hours=hours), phases=phases)
    for number, started in ((98, NOW - timedelta(days=40)), (99, NOW + timedelta(days=2))):
        completed(case, number, started=started, phases=tuple(
            (phase, 9999) for phase in ("implementing", "checking", "baseline", "test-slot-wait")))
    recorded_phase_run(case, 50, started=NOW - timedelta(seconds=610), waited=25,
                       phases=(("baseline", 25, {}), ("implementing", 60, {}), ("test-slot-wait", 25, {})),
                       active_phase="checking")

    result = facts(case, capsys)
    history = result["history"]
    assert history["runs"] == 4, "Compare against recent completed executions, excluding the current run and future records"
    expected = {"implementing_seconds": 60, "checking_seconds": 25, "baseline_seconds": 25,
                "test_slot_wait_seconds": 25, "runtime_seconds": 185}
    assert {key: history["medians"][key] for key in expected} == expected
    current, = [row for row in result["runs"] if row["issue"] == 50]
    assert current["runtime_seconds"] == 610 and current["checking_seconds"] == 500
    comparison = current["comparisons"]["checking_seconds"]
    assert comparison["median_seconds"] == 25 and comparison["ratio"] == 20
    assert comparison["above_median"] is True, (
        "Mark the measured 500s phase against its 25s median; this is an arithmetic comparison, not a call threshold")


def test_orch_facts_waste_shares_count_waits_and_explicit_repeats_without_a_history_baseline(
    tmp_path, monkeypatch, capsys,
):
    case = facts_case(tmp_path, monkeypatch)
    recorded_phase_run(case, 31, started=NOW - timedelta(hours=3), waited=30, phases=(
        ("baseline", 20, {"event_id": "base-a", "work_id": "baseline:sha-a"}),
        ("implementing", 50, {}),
        ("test-slot-wait", 10, {}),
        # A wait within a repeated operation belongs to both categories, but
        # occupies only one interval: count it once, not twice.
        ("baseline-wait", 20, {"repeat_of": "base-a"}),
        ("baseline", 20, {"event_id": "base-a-rebuilt", "work_id": "baseline:sha-a", "repeat_of": "base-a"}),
        ("checking", 80, {}),
    ))
    first = facts(case, capsys)
    only, = first["runs"]
    assert (only["runtime_seconds"], only["waste_seconds"], only["waste_share"]) == (200, 50, 0.25)
    assert first["waste"]["share"] == 0.25, "One execution is enough to measure absolute waste, without a historical norm"

    recorded_phase_run(case, 32, started=NOW - timedelta(hours=2), waited=20, outcome="timeout", phases=(
        ("implementing", 40, {"event_id": "before-timeout", "work_id": "implement:32:part-a"}),
        ("test-slot-wait", 20, {}),
        ("checking", 20, {}),
    ))
    recorded_phase_run(case, 32, started=NOW - timedelta(hours=1), waited=20, phases=(
        ("baseline-wait", 20, {}),
        # A different base is new work, even though an earlier run built a baseline.
        ("baseline", 10, {"event_id": "base-b", "work_id": "baseline:sha-b"}),
        ("implementing", 40, {"work_id": "implement:32:part-a", "repeat_of": "before-timeout", "repeat_reason": "timeout"}),
        ("implementing", 30, {"work_id": "implement:32:part-b"}),
        ("checking", 20, {}),
    ))
    result = facts(case, capsys)
    rows = {(row["issue"], row["runtime_seconds"]): row for row in result["runs"]}
    assert set(rows) == {(31, 200), (32, 80), (32, 120)}, "Keep both the timed-out execution and its retry"
    for key, seconds, share in (((31, 200), 50, 0.25), ((32, 80), 20, 0.25), ((32, 120), 60, 0.5)):
        assert rows[key]["waste_seconds"] == seconds
        assert rows[key]["waste_share"] == share
    overall = result["waste"]
    assert overall["seconds"] == 130 and overall["runtime_seconds"] == 400
    assert overall["share"] == pytest.approx(13 / 40), (
        "Divide total wasted seconds by total runtime, not the mean of run ratios; do not charge an entire timeout as lost work")


def facts_without_plan(case, capsys, monkeypatch, *, human=False):
    """Observe read-only facts while rejecting access to the unrelated plan.

    Take file snapshots outside the open guards: the test's own unchanged-file
    check must be able to read the poison plan, while the command must not.
    """
    import builtins
    import io
    from pathlib import Path

    before = files(case.files_root)
    plan_path = (case.store.root / "plan.json").resolve()

    def guarded(open_file):
        def open_local(file, *args, **kwargs):
            if isinstance(file, (str, os.PathLike)):
                assert Path(file).resolve() != plan_path, "Facts must not read a plan throughput goal number"
            return open_file(file, *args, **kwargs)
        return open_local

    capsys.readouterr()
    with monkeypatch.context() as guard:
        guard.setattr(builtins, "open", guarded(builtins.open))
        guard.setattr(io, "open", guarded(io.open))
        code = cli_exit(case.main, ["orch", "facts", "--repo-root", str(case.root)] + ([] if human else ["--json"]))
    output = capsys.readouterr()
    assert files(case.files_root) == before and case.gh_calls == []
    assert code == 0, f"fa orch facts must exist and succeed: {output.err.strip()}"
    return output.out if human else json.loads(output.out)


@pytest.mark.parametrize("kind,load,slot_count", [
    ("test_slots", 0.1, 1), ("structure", 0.1, 4), ("machine", 2.0, 4),
], ids=["test-slots-bind-with-machine-headroom", "overlap-binds-with-free-run-slots", "machine-load-binds"])
def test_orch_facts_idle_resources_and_one_binding_constraint_without_a_throughput_goal(
    tmp_path, monkeypatch, capsys, kind, load, slot_count,
):
    from foragent import capacity, scheduler

    case = facts_case(tmp_path, monkeypatch)
    with (case.root / ".foragent.yml").open("a", encoding="utf-8") as config:
        config.write("max_parallel: 4\nlanes:\n  main:\n    paths: ['src/**']\n")
    samples = []

    def machine_load():
        samples.append(("normal", load))
        return "normal", load

    monkeypatch.setattr(capacity, "machine_load", machine_load)
    monkeypatch.setattr(scheduler, "machine_load", machine_load)
    monkeypatch.setattr(capacity, "memory_pressure", lambda: "normal")
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    monkeypatch.setattr(os, "getloadavg", lambda: (8 * load, 8 * load, 8 * load))
    capacity.write_state(case.store, 4, NOW)
    capacity.update_test_slots(slot_count, NOW, pressure="normal", load_per_cpu=0)

    if kind == "test_slots":
        approved_issue(case, 1, ["src/one.py"], labels=("approved", "running"), active=True)
        approved_issue(case, 2, ["src/two.py"], labels=("approved", "running"), active=True)
        case.store.update_run(1, phase="checking", phase_started_at=NOW.isoformat())
        case.store.update_run(2, phase="test-slot-wait", phase_started_at=NOW.isoformat())
    else:
        paths = (["src/shared.py"] * 3 if kind == "structure"
                 else ["src/active.py", "src/one.py", "src/two.py"])
        approved_issue(case, 1, [paths[0]], labels=("approved", "running"), active=True)
        case.store.update_run(1, phase="implementing", phase_started_at=NOW.isoformat())
        for number, path in enumerate(paths[1:], 2):
            approved_issue(case, number, [path])

    # Same quota window: 30% -> 40% over 20 minutes, reset in 10 minutes.
    # Continuing that observed rate leaves 55% unused, rather than the 60%
    # remaining now. This estimate requires recorded usage, never a goal number.
    reset = (NOW + timedelta(minutes=10)).isoformat()
    observations = [
        {"executor": "codex", "seen_at": seen.isoformat(), "windows": [
            {"name": "5h", "used_percent": used, "resets_at": reset}]}
        for seen, used in ((NOW - timedelta(minutes=20), 30), (NOW, 40))
    ]
    case.store._write_json(case.store.root / "usage.json", {"codex": {
        key: value for key, value in observations[-1].items() if key != "executor"}})
    (case.store.root / "usage-history.jsonl").write_text(
        "".join(json.dumps(observation) + "\n" for observation in observations), encoding="utf-8")
    goal_number = 987654321
    case.store._write_json(case.store.root / "plan.json", {
        "queues": {}, "paused": {}, "written_by": "human", "expires_at": (NOW + timedelta(days=1)).isoformat(),
        "goal": {"merges_per_hour": goal_number},
    })

    result = facts_without_plan(case, capsys, monkeypatch)
    assert samples, "Read the patched machine-load sensor, without launching a process"
    resources = result["resources"]
    machine = resources["machine"]
    assert machine["load_per_cpu"] == load and machine["memory_pressure"] == "normal"
    assert machine["memory_headroom"] is True
    assert machine["idle_while_waiting"] is (kind != "machine")
    quota = resources["quota"]["codex"]
    assert quota["remaining_share"] == pytest.approx(0.6)
    assert quota["expiring_unused_share"] == pytest.approx(0.55) and quota["resets_at"] == reset
    assert quota["idle_while_waiting"] is True
    slots = resources["test_slots"]
    assert slots["queue_length"] == (1 if kind == "test_slots" else 0)
    assert slots["queue_with_machine_headroom"] is (kind == "test_slots")
    work = resources["parallelizable_work"]
    assert work["free_run_slots"] == (2 if kind == "test_slots" else 3)
    assert work["overlap_blocked"] == ([2, 3] if kind == "structure" else [])
    assert work["idle_while_waiting"] is (kind == "structure")
    binding = result["binding"]
    assert isinstance(binding, dict), "Name one current binding resource, not a list of possible causes"
    assert binding["kind"] in {"quota", "machine", "cap", "test_slots", "structure"}
    assert binding["kind"] == kind and binding["normal"] is (kind == "machine"), (
        "A saturated machine/quota is a real limit; an earlier test-slot, cap or file-overlap limit is fixable")
    assert "goal" not in result and "throughput_goal" not in result
    assert str(goal_number) not in json.dumps(result)
    human = facts_without_plan(case, capsys, monkeypatch, human=True)
    assert str(goal_number) not in human, "Show resource facts without a plan throughput target"
