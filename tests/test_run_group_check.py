from dataclasses import replace
from datetime import UTC, datetime
import json
import subprocess
from types import SimpleNamespace

import pytest

from foragent import run_group_check as group
from foragent import run_merge_turn as turn
from foragent.config import Config
from foragent.issue import body_hash
from foragent.state import StateStore


NOW = datetime(2026, 10, 31, 12, tzinfo=UTC)


def body(n):
    return f"## Goal\nMember.\n## Done when\n```sh\necho member-{n}\n```\n"


def make_ctx(tmp_path, numbers, *, maximum=4, approved=None, heads=None):
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    heads = dict(heads or {})
    for n in numbers:
        store.start_run(n, branch=f"foragent/issue-{n}", worktree=store.worktree_path(n))
        if approved is None or n in approved:
            text = body(n)
            store.save_approval(n, body=text, body_sha=body_hash(text), acceptance_blobs={},
                                approved_by="human", approved_at=NOW.isoformat())
    tracker = SimpleNamespace(find_pr=lambda branch: int(branch.rsplit("-", 1)[1]),
                              pr_head_sha=lambda pr: heads.get(pr, f"h{pr}"))
    ctx = SimpleNamespace(store=store, now=lambda: NOW, tracker=tracker,
                          config=replace(Config(), merge_group_max=maximum))
    for n in numbers:
        turn.join(ctx, turn.seat(ctx, n, "main"), f"h{n}")
    return ctx


def numbers(ctx, leader):
    return [member.n for member in group.members(ctx, turn.seat(ctx, leader, "main"), f"h{leader}")]


def test_group_counts_the_holder_and_keeps_queue_order(tmp_path):
    ctx = make_ctx(tmp_path, (5, 1, 4, 7))
    assert numbers(ctx, 5) == [5, 1, 4, 7]
    ctx.config = replace(ctx.config, merge_group_max=2)
    assert numbers(ctx, 5) == [5, 1]
    members = group.members(ctx, turn.seat(ctx, 5, "main"), "h5")
    assert [m.commands for m in members] == [("echo member-5",), ("echo member-1",)]


def test_one_disables_grouping(tmp_path):
    ctx = make_ctx(tmp_path, (5, 1), maximum=1)
    assert numbers(ctx, 5) == []


def test_dead_entries_are_skipped_and_an_ineligible_live_entry_ends_the_group(tmp_path):
    ctx = make_ctx(tmp_path, (5, 1, 4, 7), heads={4: "moved"})
    ctx.store.finish_run(1, outcome="merged")
    # 1 is dead (skipped); 4's ready head is no longer its PR head, so 7 cannot join either.
    assert numbers(ctx, 5) == []
    ctx = make_ctx(tmp_path / "b", (5, 1, 4), approved={5, 1})
    assert numbers(ctx, 5) == [5, 1]


def test_a_lone_holder_is_no_group(tmp_path):
    ctx = make_ctx(tmp_path, (5,))
    assert numbers(ctx, 5) == []


def record_result(store, verdict, count, **fields):
    from foragent import orch_events

    return orch_events.append_event(store, NOW, 99, "group-check", "result",
                                    verdict=verdict, members=[{}] * count, **fields)


def test_auto_replays_in_journal_order_and_only_uses_verdict_and_member_count(tmp_path):
    store = StateStore(tmp_path / "state")
    config = Config()
    assert group.group_limit(store, config) == (4, "auto")
    # Deliberately reverse timestamps: file order, not timestamps, defines the replay.
    for verdict, count, expected in [
        ("passed", 4, 5), ("passed", 3, 5), ("failed", 1, 4),
        ("passed", 4, 5), ("void", 8, 5), ("unknown", 8, 5),
        ("passed", 5, 6), ("passed", 6, 7), ("passed", 7, 8), ("passed", 8, 8),
        *[("failed", 1, max(2, 7 - i)) for i in range(8)],
    ]:
        event = record_result(store, verdict, count, passed=verdict != "passed")
        path = store.root / "orch-events.jsonl"
        lines = path.read_text().splitlines()
        event["at"] = str(100 - len(lines))
        lines[-1] = json.dumps(event)
        path.write_text("\n".join(lines) + "\n")
        before = path.read_bytes()
        assert group.group_limit(store, config) == (expected, "auto")
        assert group.group_limit(store, config) == (expected, "auto")
        assert path.read_bytes() == before
    # Only retained history is replayed, with no persisted limit.
    path.write_text("")
    assert group.group_limit(store, config) == (4, "auto")


def test_auto_ignores_unreadable_and_unusable_events(tmp_path):
    store = StateStore(tmp_path / "state")
    store.root.mkdir(parents=True)
    path = store.root / "orch-events.jsonl"
    path.write_text("\n".join([
        "not json", "null", "[]", "1",
        json.dumps({"kind": "other", "verdict": "failed", "members": []}),
        json.dumps({"kind": "group-check", "verdict": "failed"}),
        json.dumps({"kind": "group-check", "verdict": "passed", "members": 4}),
        json.dumps({"kind": "group-check", "passed": True, "members": [{}] * 4}),
    ]) + "\n")
    assert group.group_limit(store, Config()) == (4, "auto")
    record_result(store, "passed", 4)
    assert group.group_limit(store, Config()) == (5, "auto")
    path.unlink()
    path.mkdir()  # An unreadable journal also leaves the starting limit alone.
    assert group.group_limit(store, Config()) == (4, "auto")


@pytest.mark.parametrize("maximum", [1, 3, 12])
def test_fixed_limit_ignores_history_without_reading_it(tmp_path, monkeypatch, maximum):
    from foragent import orch_events

    store = StateStore(tmp_path / "state")
    record_result(store, "passed", 20)
    record_result(store, "failed", 1)

    def unexpected_read(*args):
        pytest.fail("a fixed limit must not read the journal")

    monkeypatch.setattr(orch_events, "events", unexpected_read)
    assert group.group_limit(store, Config(merge_group_max=maximum)) == (maximum, "fixed")


def test_members_use_the_current_auto_limit(tmp_path):
    ctx = make_ctx(tmp_path, (5, 1, 4, 7, 8, 9), maximum="auto")
    assert numbers(ctx, 5) == [5, 1, 4, 7]
    record_result(ctx.store, "passed", 4)
    assert numbers(ctx, 5) == [5, 1, 4, 7, 8]
    record_result(ctx.store, "failed", 1)
    record_result(ctx.store, "failed", 1)
    assert numbers(ctx, 5) == [5, 1, 4]


@pytest.mark.parametrize("measured,remaining,expected", [
    (736, 5000, 1472), (2000, 5000, 3600), (736, 1000, 1000), (100, 5000, 900),
    (None, 5000, 1600),
])
@pytest.mark.parametrize("timed_out", [False, True])
def test_joint_suite_derives_current_mains_baseline_for_every_entrypoint(
    tmp_path, monkeypatch, measured, remaining, expected, timed_out,
):
    from contextlib import nullcontext

    from foragent import (
        ci_mode, orch_events, run_baseline, run_baseline_limits, run_checks, run_implementation, runner,
    )
    from foragent.run_baseline import AcceptanceBaseline
    from foragent.run_checks import CheckResult

    ctx = make_ctx(tmp_path, (5, 1))
    ctx.config = replace(ctx.config, test_command="pytest -q")
    ctx.repo_root = tmp_path
    ctx.monotonic = lambda: 0
    monkeypatch.setattr(runner, "_remaining", lambda *args: remaining)
    captured, ran = [], []
    monkeypatch.setattr(run_baseline_limits, "repository_limit", lambda *args: (1600, 800))
    monkeypatch.setattr(run_checks, "_related_unit_tests", lambda *args, **kwargs: ["tests/test_related.py"])
    monkeypatch.setattr(ci_mode, "enabled", lambda ctx, target: False)
    monkeypatch.setattr(run_implementation, "_detect_test_command_at", lambda git, sha: None)
    monkeypatch.setattr(run_baseline, "_capture_acceptance_baseline", lambda n, ctx, command, ref: (
        captured.append((n, command, ref)) or AcceptanceBaseline("pytest -q", ("tests/acceptance/test_a.py::t",), duration_seconds=measured)))
    monkeypatch.setattr(group, "_combine", lambda git, base, members: "combined")
    monkeypatch.setattr(group, "_trees", lambda *args: ["tree-5", "tree-1"])
    monkeypatch.setattr(group, "_test_slot", lambda n, ctx: nullcontext())

    def check(command, worktree, log, limit, **kwargs):
        ran.append((command, limit, kwargs.get("clock")))
        return CheckResult(command, 0, timed_out and "tests/acceptance/" in command, "")

    monkeypatch.setattr(runner, "_check", check)
    ctx.git = SimpleNamespace(add_detached_worktree=lambda path, ref: None, remove_worktree=lambda path: None,
                              _call=lambda args: "")
    place = turn.seat(ctx, 5, "main")
    done = set()
    group.check(5, ctx, place, "h5", "main-sha", done)
    group.check(5, ctx, place, "h5", "main-sha", done)  # once per (main, heads)
    assert captured == [(5, "pytest -q", "main-sha")]
    assert ran == [
        ("echo member-5", 900, None),
        ("echo member-1", 900, None),
        ("pytest -q tests/acceptance/test_a.py::t", expected, ctx.monotonic),
        ("PYTEST_ADDOPTS= pytest -o addopts= --rootdir=. --maxfail=0 "
         "-p no:cacheprovider tests/test_related.py", 900, None),
    ]
    assert orch_events.has_event(ctx.store, 5, "group-check", passed=not timed_out,
                                 verdict="void" if timed_out else "passed")


@pytest.mark.parametrize("conflict_index", [0, 1, 2])
def test_combine_stops_at_first_real_conflict(git_history, monkeypatch, conflict_index):
    from conftest import git_command
    from foragent.git import GitRepo

    history = git_history
    original = history.tip
    history.update({"conflict.txt": "base\n"})
    base = history.tip
    members = []
    for index in range(4):
        history.tip = original
        git_command(history.origin, "update-ref", "refs/heads/main", original)
        history.update({"conflict.txt" if index == conflict_index else f"member-{index}": f"{index}\n"})
        members.append(group.Member(index, history.tip, ()))
    git = GitRepo(history.origin)
    call = git._call
    attempted = []

    def observe(args):
        if args[:2] == ["merge-tree", "--write-tree"]:
            attempted.append(args[-1])
        return call(args)

    monkeypatch.setattr(git, "_call", observe)
    combined = group._combine(git, base, members)
    assert attempted == [m.head_sha for m in members[:conflict_index + 1]]
    trees = group._trees(git, base, combined)
    assert len(trees) == conflict_index
    assert git_command(history.origin, "show", f"{combined}:conflict.txt") == "base"
    files = git_command(history.origin, "ls-tree", "--name-only", combined).splitlines()
    assert [f"member-{i}" in files for i in range(4)] == [i < conflict_index for i in range(4)]
    if conflict_index == 0:
        assert combined == base


def test_combine_does_not_swallow_merge_tree_infrastructure_error():
    from foragent.git import GitError

    def call(args):
        if args[0] == "merge-tree":
            raise GitError("unreadable CONFLICT object") from subprocess.CalledProcessError(128, args)
        return ""

    with pytest.raises(GitError, match="unreadable CONFLICT object"):
        group._combine(SimpleNamespace(_call=call), "base", [group.Member(5, "head", ())])
