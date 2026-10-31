from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

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


def test_joint_suite_derives_current_mains_baseline_for_every_entrypoint(tmp_path, monkeypatch):
    from contextlib import nullcontext

    from foragent import ci_mode, run_baseline, run_implementation, runner
    from foragent.run_baseline import AcceptanceBaseline
    from foragent.run_checks import CheckResult

    ctx = make_ctx(tmp_path, (5, 1))
    ctx.config = replace(ctx.config, test_command="pytest -q")
    ctx.repo_root = tmp_path
    captured, ran = [], []
    monkeypatch.setattr(ci_mode, "enabled", lambda ctx, target: False)
    monkeypatch.setattr(run_implementation, "_detect_test_command_at", lambda git, sha: None)
    monkeypatch.setattr(run_baseline, "_capture_acceptance_baseline", lambda n, ctx, command, ref: (
        captured.append((n, command, ref)) or AcceptanceBaseline("pytest -q", ("tests/acceptance/test_a.py::t",))))
    monkeypatch.setattr(group, "_combine", lambda git, base, members: "combined")
    monkeypatch.setattr(group, "_test_slot", lambda n, ctx: nullcontext())
    monkeypatch.setattr(runner, "_check", lambda command, *a, **k: ran.append(command) or CheckResult(command, 0, False, ""))
    ctx.git = SimpleNamespace(add_detached_worktree=lambda path, ref: None, remove_worktree=lambda path: None,
                              _call=lambda args: "")
    place = turn.seat(ctx, 5, "main")
    done = set()
    group.check(5, ctx, place, "h5", "main-sha", done)
    group.check(5, ctx, place, "h5", "main-sha", done)  # once per (main, heads)
    assert captured == [(5, "pytest -q", "main-sha")]
    assert ran == ["echo member-5", "echo member-1", "pytest -q tests/acceptance/test_a.py::t"]
