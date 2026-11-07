"""Acceptance test for unattended-cycle-scenario. Locked by Foragent; do not edit.

Statement: in a sandbox project, an issue that blocks on a stale locked-test expectation
reaches merged after the orchestrator publishes and approves a lock-only correction, with
no PM or person action.

The sandbox is the merge-owner sandbox: the real scheduler, runner, tracker, publication
check and merge owner, local Git, a fake GitHub service and fake executors. The scenario
replays the incident of 2026-11-06: merged main adds a case, the issue's locked test goes
stale, the blocker reaches the orchestrator, a scripted orchestrator that uses only fa
commands publishes and approves the lock-only correction, and the same issue reaches merged.

The happy path (publish, approve, lane run, review, CI, merge owner, merged) and the blocker
reaching the orchestrator already work on main. They are steps of the same scenario here,
not separate statements; PR CI and the current tests guard them.
"""

import time

from unattended_cycle_world import (CORRECTED_LOCK, LOCKED, MEMBER, PLAIN_LOCK, STALE_LOCK, FakeOrchestrator,
                                    World, main_adds_a_case, new_issue_order)
from foragent import order_publications

TEN_MINUTES = 600


def assert_no_pm_or_person(world, orchestrator=None):
    assert world.pm_questions() == [], "No question was recorded for the PM session or a person"
    assert world.pm_calls == [], "Nothing asked or answered through the PM inbox"
    assert world.person_routed() == set(), "No issue was handed to a person (needs-human)"
    if orchestrator is not None:
        assert orchestrator.questions() == []


def blocked_on_stale_lock(world, orchestrator):
    """Merged main adds a case; the issue blocks and the blocker reaches the orchestrator."""
    main_adds_a_case(world)
    assert world.drive(lambda w: "blocked" in w.labels(1), ticks=15), (
        "The issue must block: its locked test assumed two cases and merged main added a third")
    assert world.merged() == []
    blocked = orchestrator.blocked()
    assert 1 in blocked, "The blocked issue must be listed for the orchestrator"
    details = blocked[1]["details"] + blocked[1]["log_tail"]
    assert "test_known_cases" in details and "3 == 2" in details, (
        "The blocker must carry the stale locked test and its failing expectation")
    assert_no_pm_or_person(world, orchestrator)


def correct_and_finish(world, orchestrator):
    """The orchestrator's lock-only correction, then the gate and lanes run unattended."""
    orchestrator.correct_stale_lock(1, {LOCKED: CORRECTED_LOCK})
    # The gate ticks on its 300 s cadence while the publication is checked and landed.
    world.drive(lambda w: any(row["status"] != "pending" for row in order_publications.requests(w.store())),
                ticks=6, start_runs=False, step=300)
    rows = order_publications.requests(world.store())
    assert len(rows) == 1, rows
    assert rows[0]["status"] == "published", (
        "The publication check must accept the lock-only correction on the evidence of the "
        f"issue branch; it was {rows[0]['status']}: {(rows[0]['reason'] or '')[:400]}")
    orchestrator.resume(1)
    # fa commands run on the real clock, the gate on its own: each gate tick re-lists GitHub.
    assert world.drive(lambda w: w.merged(), ticks=15, step=300), (
        "After the correction the same issue must reach merged: " + world.store().log_path(1).read_text()[-2000:])


def test_stale_lock_issue_reaches_merged_after_the_orchestrator_publishes_and_approves_a_lock_only_correction(
        tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch, lock=STALE_LOCK)
    orchestrator = FakeOrchestrator(world)
    blocked_on_stale_lock(world, orchestrator)
    correct_and_finish(world, orchestrator)
    state = world.state()
    assert [row["subject"] for row in state["merges"]] == ["Member 1 (#1)"], "Merged exactly once"
    assert state["issues"]["1"]["state"] == "closed" and state["closures"].count(1) == 1
    assert world.main_file(LOCKED) == CORRECTED_LOCK.rstrip("\n"), "Main keeps the corrected lock"
    assert world.main_file(MEMBER) == "VALUE = 1\nHANDLED = 3"
    assert [row["status"] for row in order_publications.requests(world.store())] == ["published"]
    assert all(cmd[:2] != ["order", "publish"] or "--approve" in cmd for cmd in orchestrator.commands)
    assert sum(cmd[:2] == ["order", "publish"] for cmd in orchestrator.commands) == 1, (
        "One correction, published and approved once")
    assert_no_pm_or_person(world, orchestrator)


def test_one_order_runs_the_whole_cycle_and_recovers_from_a_stale_lock_with_only_fakes_within_ten_minutes(
        tmp_path, monkeypatch):
    started = time.monotonic()
    (tmp_path / "plain").mkdir()
    (tmp_path / "stale").mkdir()
    # The happy path: publish, approve, lane run, review, CI, merge owner, merged.
    plain = World(tmp_path / "plain", monkeypatch, lock=PLAIN_LOCK, seed_issue=False)
    orchestrator = FakeOrchestrator(plain)
    orchestrator.publish(new_issue_order(plain))
    assert plain.drive(lambda w: w.merged(), ticks=20, step=300), "The approved issue must reach merged unattended"
    state = plain.state()
    child = next(int(n) for n, issue in state["issues"].items() if issue["title"] == "Member 7")
    assert [row["subject"] for row in state["merges"]] == [f"Member 7 (#{child})"]
    assert plain.main_file("src/member_7.py") == "VALUE = 7"
    turns = plain.executor_turns()
    assert {"issue": child, "executor": "claude"} in turns and {"issue": child, "executor": "codex"} in turns
    assert any(row["run"]["head_sha"] for row in state["ci"].values())
    assert_only_fakes(plain)
    assert_no_pm_or_person(plain, orchestrator)
    monkeypatch.undo()  # The second sandbox project gets its own isolation.
    # The recovery path: the stale lock blocks, the orchestrator corrects it, the issue merges.
    stale = World(tmp_path / "stale", monkeypatch, lock=STALE_LOCK)
    orchestrator = FakeOrchestrator(stale)
    blocked_on_stale_lock(stale, orchestrator)
    correct_and_finish(stale, orchestrator)
    assert_only_fakes(stale)
    elapsed = time.monotonic() - started
    assert elapsed < TEN_MINUTES, f"The scenario took {elapsed:.0f} s"


def assert_only_fakes(world):
    assert world.network_calls == [], "No network call"
    # Every model turn went to a fake executor; every GitHub call went to the fake service.
    turns = world.executor_turns()
    assert turns and {turn["executor"] for turn in turns} <= {"claude", "codex"}
    assert world.reader_turns and all(turn["read_only"] for turn in world.reader_turns)
    assert all(args[0] in {"api", "pr", "issue"} for args in world.state()["calls"])
    assert_no_pm_or_person(world)
