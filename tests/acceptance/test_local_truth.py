"""Acceptance test for local-truth. Locked by Foragent; do not edit."""

from dataclasses import asdict, is_dataclass
from copy import deepcopy
from datetime import datetime, timedelta
import json
import subprocess

import pytest

from test_orch_review_handoff import bind_cli, make_case, tick_context
from test_skills_autosync import isolated_home


def local_rows(store):
    """Contract: local_issues() exposes records with pr_number and updated_at."""
    reader = getattr(store, "local_issues", None)
    assert callable(reader), "StateStore needs a public local_issues() snapshot API"
    rows = [asdict(row) if is_dataclass(row) else dict(row) for row in reader()]
    required = {"number", "title", "labels", "lane", "state", "pr_number", "merged", "updated_at"}
    for row in rows:
        assert required <= row.keys(), f"Incomplete local issue record: {row}"
        assert datetime.fromisoformat(row["updated_at"]).utcoffset() is not None
    assert len({row["number"] for row in rows}) == len(rows)
    return {row["number"]: row for row in rows}


def record_gh(monkeypatch, reply=None):
    """Guard the shared CLI boundary, including tracker, init and board calls."""
    calls = []
    original = subprocess.run

    def run(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and argv and str(argv[0]) == "gh":
            calls.append(list(argv))
            if reply is None:
                pytest.fail(f"Local observation called GitHub: {argv}")
            return subprocess.CompletedProcess(argv, 0, stdout=reply(argv[1:]), stderr="")
        return original(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def local_cli(case, monkeypatch):
    """Keep real GhTracker reads observable instead of hiding them in the fake."""
    from foragent import cli, orch
    from foragent.tracker import GhTracker

    main = bind_cli(case, monkeypatch)
    monkeypatch.setattr(cli, "GhTracker", GhTracker)
    monkeypatch.setattr(orch.time, "sleep", lambda seconds: pytest.fail(
        "The initial orch watch emission must return without polling"))
    return main


def snapshot_case(git_history, tmp_path, monkeypatch):
    """Load issues, titles, PR state and native order edges via a real gate tick."""
    from foragent.scheduler import tick

    lanes = {slug: {"paths": [f"{slug}/**"], "title": slug.title(), "emoji": "🧩"}
             for slug in ("api", "review", "decision")}
    case = make_case(git_history, tmp_path, monkeypatch, lanes=lanes, language="en",
                     skill_libraries=[], test_command="true")
    case.tracker.add_labels(1, ["lane:api"])
    case.store.update_run(1, lane="api", phase="implementing")
    case.tracker.add_issue(2, "Review dispute", labels=["blocked", "lane:review"])
    case.tracker.comment(2, "<!-- foragent:blocked reason=spec-question -->\nClarify locale")
    case.tracker.add_issue(3, "Choose locale", labels=["needs-human", "lane:decision"])
    case.tracker.add_issue(4, "Finished API task", labels=["lane:api"], state="closed")
    pr = case.tracker.create_pr(head="foragent/issue-4", base="main", title="Finished API task",
                                body="Closes #4")
    case.tracker.merge_pr(pr, expected_head_sha=case.tracker.pr_head_sha(pr))
    case.store.start_run(4, branch="foragent/issue-4", worktree=case.store.worktree_path(4), lane="api")
    case.store.finish_run(4, outcome="merged")
    case.tracker.add_issue(99, "Ship the local dashboard")
    for number in (1, 2, 3, 4):
        case.tracker.set_parent(number, 99)
    tick(tick_context(case, start_runs=False))
    return case


def test_local_truth_runner_records_pr_merge_close_and_label_removal_immediately(
    git_history, tmp_path, monkeypatch,
):
    from foragent.runner import run_issue
    from foragent.scheduler import tick
    from foragent.state import StateStore

    record_gh(monkeypatch)
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true")
    case.tracker.add_labels(1, ["ready"])
    tick(tick_context(case, start_runs=False))
    observed = {}

    def capture(stage):
        # Read another store instance so an in-memory cache cannot satisfy this.
        reader = getattr(StateStore(case.store.root), "local_issues", None)
        observed[stage] = deepcopy(list(reader())) if callable(reader) else None

    merge = case.tracker.merge_pr

    def merge_pr(number, **kwargs):
        # Default local review precedes PR creation. At merge entry, the
        # runner has returned from creating and recording the unmerged PR.
        capture("pr-created")
        case.clock[0] += timedelta(seconds=1)
        return merge(number, **kwargs)

    close = case.tracker.close_issue

    def close_issue(number, comment=None):
        capture("merged-before-close")
        close(number, comment)

    monkeypatch.setattr(case.tracker, "merge_pr", merge_pr)
    monkeypatch.setattr(case.tracker, "close_issue", close_issue)
    assert run_issue(1, case.ctx) == "merged"
    row = local_rows(StateStore(case.store.root))[1]
    pr = case.tracker.created_prs[0].number
    for stage, merged in (("pr-created", False), ("merged-before-close", True)):
        assert observed[stage] is not None, f"Snapshot missing at {stage}"
        rows = [asdict(item) if is_dataclass(item) else item for item in observed[stage]]
        during = next(item for item in rows if item["number"] == 1)
        assert during["pr_number"] == pr and during["merged"] is merged
    assert row["title"] == "Implement feature" and row["lane"] == "main"
    assert row["pr_number"] == pr and row["merged"] is True and row["state"] == "closed"
    assert set(row["labels"]) == set(case.tracker.get_issue(1).labels) == {"approved"}
    assert datetime.fromisoformat(row["updated_at"]) == case.clock[0]


def test_local_truth_runner_block_and_orchestrator_retry_update_labels_without_a_tick(
    git_history, tmp_path, monkeypatch,
):
    from foragent import orch
    from foragent.runner import run_issue
    from foragent.scheduler import tick

    record_gh(monkeypatch)
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true")
    tick(tick_context(case, start_runs=False))

    def question(kwargs):
        path = kwargs["worktree"] / ".foragent/QUESTION.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text("Which locale?\nA: English (recommended)\nB: Korean\n")

    case.implementer.steps = [question]
    assert run_issue(1, case.ctx) == "spec-question"
    blocked = local_rows(case.store)[1]
    assert "blocked" in blocked["labels"] and "running" not in blocked["labels"]
    assert blocked["state"] == "open" and blocked["pr_number"] is None and blocked["merged"] is False
    case.clock[0] += timedelta(seconds=1)
    orch.retry(case.ctx, 1)
    retried = local_rows(case.store)[1]
    assert set(retried["labels"]) == {"approved", "ready"}
    assert datetime.fromisoformat(retried["updated_at"]) == case.clock[0]


def test_local_truth_orch_watch_and_list_read_local_blocked_human_and_merged_sets(
    git_history, tmp_path, monkeypatch, capsys,
):
    case = snapshot_case(git_history, tmp_path, monkeypatch)
    main = local_cli(case, monkeypatch)
    # These direct human changes must remain invisible until a gate refresh.
    case.tracker.remove_labels(2, ["blocked"])
    case.tracker.add_labels(3, ["blocked"])
    calls = record_gh(monkeypatch)
    assert main(["orch", "watch", "--interval", "1"]) == 0
    watched = json.loads(capsys.readouterr().out)
    assert watched["blocked"] == [2] and watched["needs_human"] == [3] and watched["merged"] == [4]
    assert main(["orch", "list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [(item["number"], item["title"], item["lane"]) for item in listed] == [
        (2, "Review dispute", "review")]
    assert listed[0]["reason"] == "spec-question" and "Clarify locale" in listed[0]["details"]
    assert calls == []
    rows = local_rows(case.store)
    assert watched["blocked"] == sorted(n for n, row in rows.items()
                                         if row["state"] == "open" and "blocked" in row["labels"])
    assert watched["needs_human"] == sorted(n for n, row in rows.items()
                                             if row["state"] == "open" and "needs-human" in row["labels"])
    assert watched["merged"] == sorted(n for n, row in rows.items() if row["merged"])


def test_local_truth_gate_refreshes_remote_changes_only_after_five_minutes(
    git_history, tmp_path, monkeypatch,
):
    from foragent.scheduler import tick

    record_gh(monkeypatch)
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true")
    listings = []
    for name in ("list_issues", "list_open_issues"):
        original = getattr(case.tracker, name)

        def listing(*args, _name=name, _original=original, **kwargs):
            listings.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(case.tracker, name, listing)
    ctx = tick_context(case, start_runs=False)
    tick(ctx)
    first = len(listings)
    assert first > 0, "A fresh gate must fetch its initial issue snapshot"
    case.clock[0] += timedelta(minutes=1)
    case.tracker.record_label(1, "needs-human", "human")
    tick(ctx)
    assert len(listings) == first, "The second tick repeated a full GitHub listing within five minutes"
    assert "needs-human" not in local_rows(case.store)[1]["labels"]
    case.clock[0] += timedelta(minutes=4, seconds=1)
    tick(ctx)
    assert len(listings) > first, "The gate did not refresh after five minutes"
    assert "needs-human" in local_rows(case.store)[1]["labels"]


def test_local_truth_person_change_on_github_wins_over_foragent_local_record(
    git_history, tmp_path, monkeypatch,
):
    from foragent import orch
    from foragent.scheduler import tick
    from foragent.state import StateStore

    record_gh(monkeypatch)
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true")
    case.store.finish_run(1, outcome="spec-question")
    case.tracker.remove_labels(1, ["running"])
    case.tracker.add_labels(1, ["blocked"])
    case.tracker.record_label(1, "approved", "human")
    case.tracker.add_issue(2, "A person will close this issue")
    ctx = tick_context(case, start_runs=False)
    tick(ctx)

    writes = []
    for name in ("add_labels", "remove_labels"):
        original = getattr(case.tracker, name)

        def write(number, labels, _name=name, _original=original):
            labels = list(labels)
            writes.append((number, _name, labels))
            return _original(number, labels)

        monkeypatch.setattr(case.tracker, name, write)

    case.clock[0] += timedelta(seconds=1)
    orch.retry(case.ctx, 1)
    rows = local_rows(StateStore(case.store.root))
    assert set(rows[1]["labels"]) == set(case.tracker.get_issue(1).labels) == {"approved", "ready"}
    assert rows[2]["state"] == "open"
    assert any(number == 1 and action == "add_labels" for number, action, _ in writes)

    case.clock[0] += timedelta(seconds=1)
    with monkeypatch.context() as human:
        human.setattr(case.tracker, "actor", "human")
        case.tracker.remove_labels(1, ["ready"])
        case.tracker.record_label(1, "needs-human", "human")
        case.tracker.close_issue(2)
    human_issues = {number: case.tracker.get_issue(number) for number in (1, 2)}
    before_refresh = len(writes)

    # Check the first refresh and later ticks, including another refresh window.
    for elapsed in (timedelta(minutes=5), timedelta(seconds=1), timedelta(minutes=5)):
        case.clock[0] += elapsed
        tick(ctx)
        rows = local_rows(StateStore(case.store.root))
        for number, expected in human_issues.items():
            remote = case.tracker.get_issue(number)
            assert (rows[number]["title"], rows[number]["state"], set(rows[number]["labels"])) == (
                expected.title, expected.state, set(expected.labels))
            assert remote == expected, "A later tick overwrote the person's GitHub change"
        assert writes[before_refresh:] == [], "A tick re-applied Foragent's earlier label writes"


def test_local_truth_fresh_watchers_stay_offline_until_first_gate_tick(
    git_history, tmp_path, monkeypatch, capsys,
):
    from foragent.scheduler import tick

    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true")
    main = local_cli(case, monkeypatch)
    calls = record_gh(monkeypatch)
    assert main(["orch", "watch", "--interval", "1"]) == 0
    watched = json.loads(capsys.readouterr().out)
    assert watched["blocked"] == watched["needs_human"] == watched["merged"] == []
    assert main(["orch", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert calls == [] and local_rows(case.store) == {}
    tick(tick_context(case, start_runs=False))
    assert local_rows(case.store)[1]["title"] == "Implement feature"
