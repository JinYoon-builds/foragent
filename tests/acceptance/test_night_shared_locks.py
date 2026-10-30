"""Acceptance test for night-shared-locks. Locked by Foragent; do not edit."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest

from conftest import BASE_TIME
from foragent import order
from foragent.scheduler import TickContext, tick
from foragent.state import StateStore
from test_local_truth import record_gh
from test_order import FILE, offline_publication, plan, preread_done
from test_publish_out_of_scope import publish_cli
from test_skills_autosync import isolated_home


OTHER = "tests/acceptance/test_unrelated_night_feature.py"


@pytest.fixture
def shared_locks(offline_publication, plan, monkeypatch):
    root, config, tracker, git, calls, origin = offline_publication
    config = replace(config, approvers=["human"], skill_libraries=[])
    clock = [BASE_TIME + timedelta(hours=1)]
    tracker._now = lambda: clock[0]
    store = StateStore(root / ".foragent/state", now=lambda: clock[0])
    monkeypatch.setattr(StateStore, "for_repo", classmethod(lambda cls, repo, **kwargs: store))
    plan["issues"][1]["done_when"] = [f"uv run pytest -q {FILE}"]
    plan["issues"][1]["blocked_by"] = []
    third = deepcopy(plan["issues"][0])
    third.update(key="unrelated", title="Unrelated approved issue", lane="main", blocked_by=[],
                 done_when=[f"uv run pytest -q {OTHER}"],
                 acceptance_files={OTHER: "def test_unrelated():\n    assert True\n"})
    plan["issues"].append(third)
    report = order.publish(plan, root, config, tracker, repo="owner/repo", git=git, approve=True)
    numbers = {item["key"]: row["number"] for item, row in zip(plan["issues"], report.rows, strict=True)}
    ctx = TickContext(repo="owner/repo", repo_root=root, tracker=tracker, store=store, config=config,
                      git=git, now=lambda: clock[0], launcher=lambda number: None, start_runs=False,
                      runner_process_exists=lambda number: False)
    tick(ctx)
    assert all(store.load_approval(n) is not None for n in numbers.values())
    assert all("ready" in tracker.get_issue(n).labels for n in numbers.values())
    change = deepcopy(plan)
    change.update(id="night-shared-lock-change", title="Change a shared locked test", edits=[])
    item = deepcopy(plan["issues"][0])
    item.update(key="change", title="Shared test amendment", blocked_by=[])
    item["acceptance_files"][FILE] = "def test_login():\n    assert 1 == 1\n"
    change["issues"] = [item]
    path = root / "CHANGE.json"
    path.write_text(json.dumps(change))
    clock[0] += timedelta(hours=1)
    main = publish_cli((root, config, tracker, git, calls), monkeypatch)
    gh = record_gh(monkeypatch)
    yield SimpleNamespace(root=root, tracker=tracker, git=git, store=store, ctx=ctx, plan=change,
                          path=path, main=main, numbers=numbers)
    assert gh == [], "Shared-lock publication must use the offline tracker"


def invoke(case, capsys, *options):
    capsys.readouterr()
    code = case.main(["order", "publish", str(case.path), "--repo-root", str(case.root), *options])
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def assert_affected_list(case, message):
    assert all(f"#{case.numbers[key]}" in message for key in ("api", "client")), message
    assert f"#{case.numbers['unrelated']}" not in message, message
    assert FILE in message, message


def test_dry_run_lists_every_approved_owner_of_changed_locked_bytes(shared_locks, capsys):
    case = shared_locks
    old = (case.root / FILE).read_bytes()
    code, message = invoke(case, capsys, "--dry-run")
    assert_affected_list(case, message)
    assert (case.root / FILE).read_bytes() == old
    assert case.tracker.get_issue(case.numbers["unrelated"]).labels >= {"approved", "ready"}


def test_publish_refreshes_all_affected_snapshots_or_refuses_before_changing_locks(shared_locks, capsys):
    case = shared_locks
    old = (case.root / FILE).read_bytes()
    old_blob = case.git.blob_sha("origin/main", FILE)
    snapshots = {n: case.store.load_approval(n) for n in case.numbers.values()}
    issues = dict(case.tracker.issues)
    code, message = invoke(case, capsys)
    assert_affected_list(case, message)
    if code:
        assert (case.root / FILE).read_bytes() == old
        assert case.git.blob_sha("origin/main", FILE) == old_blob
        assert case.tracker.issues == issues
        assert {n: case.store.load_approval(n) for n in snapshots} == snapshots
    else:
        assert (case.root / FILE).read_text() == case.plan["issues"][0]["acceptance_files"][FILE]
        current = case.git.blob_sha("origin/main", FILE)
        for key in ("api", "client"):
            assert case.store.load_approval(case.numbers[key]).acceptance_blobs[FILE] == current
            assert case.store.load_approval(case.numbers[key]).acceptance_blobs[FILE] != snapshots[case.numbers[key]].acceptance_blobs[FILE]
    # A subsequent ordinary tick must not silently invalidate either owner,
    # whether publication succeeded or safely refused the shared change.
    tick(case.ctx)
    for key in ("api", "client"):
        number = case.numbers[key]
        assert case.store.load_approval(number).acceptance_blobs[FILE] == case.git.blob_sha("origin/main", FILE)
        assert case.tracker.get_issue(number).labels >= {"approved", "ready"}
        assert case.store.load_flag(number) is None
    unrelated = case.numbers["unrelated"]
    assert case.store.load_approval(unrelated) == snapshots[unrelated]
    assert case.tracker.get_issue(unrelated).labels >= {"approved", "ready"}


def test_identical_locked_bytes_do_not_require_reapproval(shared_locks, capsys):
    case = shared_locks
    case.plan["issues"][0]["acceptance_files"][FILE] = (case.root / FILE).read_text()
    case.path.write_text(json.dumps(case.plan))
    code, message = invoke(case, capsys)
    assert code == 0, message
    tick(case.ctx)
    for number in case.numbers.values():
        assert case.tracker.get_issue(number).labels >= {"approved", "ready"}
        assert case.store.load_flag(number) is None
