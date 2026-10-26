"""Acceptance test for union-merge-overlap. Locked by Foragent; do not edit."""

from dataclasses import replace
from datetime import timedelta
import json
from types import SimpleNamespace

from conftest import git_command
from foragent import run_overlap
from foragent.issue import parse_body
from test_lane_width_by_overlap import _finish, _wide_lane, ctx  # noqa: F401
from test_optimistic_overlap import (
    CLI,
    OTHER,
    _change,
    _issue,
    _peer,
    _runner_case,
    _tick,
    isolated_home,  # noqa: F401
    offline_acceptance,  # noqa: F401
    offline_overlap,  # noqa: F401
)


MEMORY = ".claude/agent-memory/orch/MEMORY.md"
UNION_INDEX = "docs/shared-index.md"
NORMAL = "src/foragent/shared.py"
FREE = "src/foragent/free.py"


def _union_base(history):
    history.update({
        ".gitattributes": (
            ".claude/agent-memory/**/MEMORY.md merge=union\n"
            f"{UNION_INDEX} merge=union\n"
        ),
        MEMORY: "# Lane memory\n",
        UNION_INDEX: "# Shared index\n",
        NORMAL: "VALUE = 0\n",
    })
    git_command(history.clone, "fetch", "origin", "main")
    git_command(history.clone, "merge", "--ff-only", "origin/main")
    for path, value in ((MEMORY, "union"), (UNION_INDEX, "union"), (NORMAL, "unspecified")):
        assert git_command(history.clone, "check-attr", "merge", "--", path) == (
            f"{path}: merge: {value}"
        )


def _late_pair(git_history, tmp_path, monkeypatch, paths):
    _union_base(git_history)
    case = _runner_case(git_history, tmp_path, monkeypatch, [CLI], overlap_per_file=1)
    _peer(case, [OTHER])
    # Both runs have their own commit before appending paths outside declared Files.
    for number, declared in ((1, CLI), (2, OTHER)):
        worktree = _change(case.ctx, number, {declared: f"VALUE = {number}\n"})
        _change(case.ctx, number, {
            path: (worktree / path).read_text() + f"# Appended by issue {number}\n"
            for path in paths
        }, commit=False)
    return case


def _wait_for_pair(case, monkeypatch):
    entered, sleeps = [], []
    credited_wait = run_overlap._credited_wait

    def observe(number, context, acquire):
        entered.append(number)
        return credited_wait(number, context, acquire)

    def release_holder(seconds):
        assert not sleeps, "Releasing the earlier holder must end the late-file wait"
        assert case.store.get_run(1).status == "running"
        assert case.store.get_run(2).waiting_until is not None
        sleeps.append(seconds)
        case.clock[0] += timedelta(seconds=seconds)
        case.store.finish_run(1, outcome="merged")
        case.tracker.close_issue(1)
        case.tracker.remove_labels(1, ["ready", "running"])

    monkeypatch.setattr(run_overlap, "_credited_wait", observe)
    case.ctx.sleep = release_holder
    for number in (1, 2):
        approval = case.store.load_approval(number)
        run_overlap.wait_for_overlap(SimpleNamespace(
            ctx=case.ctx, n=number, parsed=parse_body(approval.body), target_branch="main",
        ))
    return entered, sleeps


def test_union_merge_overlap_late_memory_appends_never_enter_wait(git_history, tmp_path, monkeypatch):
    case = _late_pair(git_history, tmp_path, monkeypatch, [MEMORY])

    entered, sleeps = _wait_for_pair(case, monkeypatch)

    assert entered == [], f"merge=union MEMORY.md entered the late-claim wait for issues {entered}"
    assert sleeps == []
    for number in (1, 2):
        saved = json.loads((case.store.root / "runs" / f"{number}.json").read_text())
        assert MEMORY not in saved["declared_files"]
        assert MEMORY in saved["changed_files"], "Exercise a real late memory append"
        assert saved["overlap_records"] == [], "Union memory appends must not produce overlap facts"


def test_union_merge_overlap_normal_file_still_waits_and_only_it_is_recorded(
    git_history, tmp_path, monkeypatch,
):
    # An unrelated union path proves the repository attribute, rather than a
    # hardcoded memory-path exemption, controls which overlap facts are kept.
    case = _late_pair(git_history, tmp_path, monkeypatch, [NORMAL, UNION_INDEX])

    entered, sleeps = _wait_for_pair(case, monkeypatch)

    assert entered == [2], "The later normal-source append must still wait for the earlier run"
    assert len(sleeps) == 1, "Wait with the fake clock until the earlier holder finishes"
    for number, peer in ((1, 2), (2, 1)):
        saved = json.loads((case.store.root / "runs" / f"{number}.json").read_text())
        rows = saved["overlap_records"]
        normal = {"file": NORMAL, "issues": [peer], "conflicted": False,
                  "resolution_seconds": 0, "redo_count": 0}
        assert normal in rows, "Keep today's ordinary-file overlap evidence in both runs"
        assert rows == [normal], f"merge=union path leaked into issue {number}'s overlap_records: {rows}"


def test_union_merge_overlap_declared_union_and_free_path_start_at_cap(ctx, git_history):
    _union_base(git_history)
    starts = _wide_lane(ctx, 3)
    ctx.config = replace(ctx.config, overlap_per_file=1)
    _issue(ctx, 1, [MEMORY], running=True)
    _issue(ctx, 2, [MEMORY, FREE])
    # Admission uses the run's base attributes, even if a local edit disagrees.
    (ctx.repo_root / ".gitattributes").write_text(f"{MEMORY} merge=text\n")
    assert git_command(ctx.repo_root, "check-attr", "merge", "--", MEMORY).endswith("merge: text")
    assert git_command(
        ctx.repo_root, "check-attr", "--source=origin/main", "merge", "--", MEMORY,
    ).endswith("merge: union")

    report = _tick(ctx)

    assert starts == [2], f"Union-only holder consumed the per-file cap: {report.actions}"
    assert {run.number for run in ctx.store.active_runs()} == {1, 2}
    assert "running" in ctx.tracker.get_issue(2).labels


def test_union_merge_overlap_exclusive_wins_until_released(ctx, git_history):
    _union_base(git_history)
    starts = _wide_lane(ctx, 4)
    ctx.config = replace(ctx.config, overlap_per_file=1)
    _issue(ctx, 1, [MEMORY], running=True, exclusive=[MEMORY])
    _issue(ctx, 2, [MEMORY, FREE])
    _issue(ctx, 3, [MEMORY], running=True)

    _tick(ctx)

    assert starts == [], "An exclusive reservation must lock even a merge=union path"
    assert "ready" in ctx.tracker.get_issue(2).labels
    assert "running" not in ctx.tracker.get_issue(2).labels
    assert ctx.store.get_run(2) is None
    _finish(ctx, 1)

    report = _tick(ctx)

    assert starts == [2], (
        f"Released exclusive lock still blocked by an ordinary union holder: {report.actions}"
    )
    assert {run.number for run in ctx.store.active_runs()} == {2, 3}
