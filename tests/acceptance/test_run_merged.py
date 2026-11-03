"""Acceptance test for run-merged. Locked by Foragent; do not edit.

Under the merge owner a worker run finishes as ``merge-queued`` and the owner
later records the GitHub merge of that run. Every reader that asks "did this
run merge" must then agree with the store's one merge rule: such a run counts
as merged, and a ``merge-queued`` run without a recorded merge does not.
"""

from datetime import timedelta
import os

from test_orch_facts import NOW, facts, facts_case
from test_skills_autosync import isolated_home  # noqa: F401  (needed by the star fixtures)
from test_star_prompt import (  # noqa: F401  (autouse fixture: offline gh, machine root)
    assert_no_offer, assert_offer, briefing, offline_star_environment, record_merges, star_case,
)


def owner_merged(store, clock, number, *, finished, merge=True):
    """A run the worker left queued, then (optionally) the owner's recorded merge of it."""
    clock[0] = finished - timedelta(minutes=10)
    run = store.start_run(number, branch=f"foragent/issue-{number}", worktree=store.worktree_path(number))
    clock[0] = finished
    store.finish_run(number, outcome="merge-queued")
    if merge:
        merged_at = finished + timedelta(minutes=1)
        store.record_owner_merge(number, run.started_at, {
            "pr": 1000 + number, "target": "main", "head": f"{number:040x}", "merged_at": merged_at.isoformat()})
    return run


def merged_by_worker(case, number, *, finished):
    case.clock[0] = finished - timedelta(minutes=10)
    case.store.start_run(number, branch=f"foragent/issue-{number}", worktree=case.store.worktree_path(number))
    case.clock[0] = finished
    case.store.finish_run(number, outcome="merged")


def owner_facts_case(tmp_path, monkeypatch):
    case = facts_case(tmp_path, monkeypatch)
    (case.root / ".foragent.yml").write_text(
        "repo: owner/repo\nlanguage: en\nskill_libraries: []\nnotify: none\ntest_slots: 4\n"
        "merge_owner: true\ntest_command: pytest -q\n")
    # One open issue waits, so a stop in merges is a stall.
    case.store.update_local_issue(90, title="Queued work", labels=[], state="open", body="")
    return case


def test_run_merged_facts_count_owner_merges_for_stall_and_failures(tmp_path, monkeypatch, capsys):
    case = owner_facts_case(tmp_path, monkeypatch)
    # Merges by the worker stopped hours ago, at a 10-minute rate ...
    merged_by_worker(case, 1, finished=NOW - timedelta(hours=10))
    merged_by_worker(case, 2, finished=NOW - timedelta(hours=10) + timedelta(minutes=10))
    # ... and the merge owner has merged since, at the same rate, just now.
    owner_merged(case.store, case.clock, 3, finished=NOW - timedelta(minutes=22))
    owner_merged(case.store, case.clock, 4, finished=NOW - timedelta(minutes=12))
    case.clock[0] = NOW
    result = facts(case, capsys)
    assert "merge_stall" not in result["kinds"], (
        "Owner merges are merges: the merge interval must include them, so recent owner merges are no stall")
    assert result["failure_reasons"] == {}, (
        "A run the owner merged is not a failure, whatever outcome the worker left on it")


def test_run_merged_facts_stall_ignores_queued_runs_the_owner_has_not_merged(tmp_path, monkeypatch, capsys):
    case = owner_facts_case(tmp_path, monkeypatch)
    # Only owner merges exist, 10 minutes apart, and they stopped hours ago.
    owner_merged(case.store, case.clock, 1, finished=NOW - timedelta(hours=10))
    owner_merged(case.store, case.clock, 2, finished=NOW - timedelta(hours=10) + timedelta(minutes=10))
    # Recent runs are still queued: no merge was recorded for them.
    owner_merged(case.store, case.clock, 3, finished=NOW - timedelta(minutes=22), merge=False)
    owner_merged(case.store, case.clock, 4, finished=NOW - timedelta(minutes=12), merge=False)
    case.clock[0] = NOW
    result = facts(case, capsys)
    assert "merge_stall" in result["kinds"], (
        "Owner merges set the merge rate; queued runs without a recorded merge do not reset it")
    numbers = result["kind_numbers"]["merge_stall"]
    assert numbers["merge_interval_minutes"] == 10, "Each owner-merged run is one merge, counted once"
    assert numbers["queued"] >= 1


def test_run_merged_log_retention_treats_owner_merged_runs_as_merged(tmp_path):
    from conftest import BASE_TIME
    from foragent.state import StateStore

    clock = [BASE_TIME]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    finished = BASE_TIME - timedelta(days=8)
    logs = {}
    for number, merge in ((1, True), (2, False)):
        owner_merged(store, clock, number, finished=finished, merge=merge)
        path = logs[number] = store.log_path(number)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 20)
        os.utime(path, (finished.timestamp(), finished.timestamp()))
    clock[0] = BASE_TIME
    removed = store.prune_logs(BASE_TIME)
    assert removed == [logs[1]] and not logs[1].exists(), (
        "An owner-merged run's logs follow the merged retention (7 days by default)")
    assert logs[2].is_file(), "A queued run without a recorded merge keeps the non-merged retention"


def test_run_merged_star_prompt_counts_owner_merges(star_case, capsys):
    case = star_case
    record_merges(case, range(1, 10))
    case.clock[0] += timedelta(seconds=1)
    owner_merged(case.store, case.clock, 50, finished=case.clock[0], merge=False)
    assert_no_offer(briefing(case, capsys), "A queued run the owner has not merged is not a merge")
    case.clock[0] += timedelta(minutes=5)
    owner_merged(case.store, case.clock, 51, finished=case.clock[0])
    assert_offer(briefing(case, capsys), "The tenth distinct merge was made by the merge owner")
