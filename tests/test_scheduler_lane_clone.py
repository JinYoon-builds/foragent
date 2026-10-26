from dataclasses import replace
from datetime import timedelta, timezone
import json
import os
from pathlib import Path

import pytest

from foragent.scheduler import (
    _can_start_in_lane, _file_patterns_overlap, _filesystem_case_sensitive, tick,
)

from test_scheduler import BODY, NOW, approve, ctx


@pytest.mark.parametrize("left,right,expected", [
    ("src/watch.py", "src/watch.py", True),
    ("src/watch.py", "src/state.py", False),
    ("src/watch*.py", "src/watch_data.py", True),
    ("src/watch*.py", "src/*data.py", True),
    ("src/*a.py", "src/*b.py", False),
    ("src/watch?.py", "src/watch.py", False),
    ("src/watch?.py", "src/watch*.py", True),
    ("src/watch[0-9].py", "src/watch[5-8].py", True),
    ("src/watch[0-4].py", "src/watch[5-9].py", False),
    ("src/watch[!0-9].py", "src/watch[0-9].py", False),
    ("src/watch[!0-9].py", "src/watch?.py", True),
    ("src/[a-z].py", "src/[m-한].py", True),
    ("src/[한-힣].py", "src/[!한-힣].py", False),
    ("src/[z-a].py", "src/*.py", False),
    ("src/[[]name.py", "src/[name.py", True),
    ("src/[]].py", "src/?.py", True),
    ("src/[!].py", "src/*.py", True),
    ("src/**/watch.py", "src/watch.py", True),
    ("src/**/watch.py", "src/pkg/deep/watch.py", True),
    ("src/*/watch.py", "src/pkg/deep/watch.py", False),
    ("src/**/watch*.py", "src/pkg/**/watch_data.py", True),
    ("src/**/watch*.py", "src/**/state.py", False),
    ("src/**", "docs/**", False),
    ("./src/watch.py", "src/watch.py", True),
])
def test_file_patterns_overlap_is_symmetric(left, right, expected):
    assert _file_patterns_overlap(left, right) is expected
    assert _file_patterns_overlap(right, left) is expected


def _issue(ctx, number, files, *, running=False):
    ctx.config = replace(ctx.config, overlap_per_file=1)
    body = BODY
    if files is not None:
        body += "\n## Files\n" + "".join(f"- {path}\n" for path in files)
    approve(ctx, number, body=body, labels=["ready"])
    if running:
        start_runs = ctx.start_runs
        ctx.start_runs = False
        try:
            tick(ctx)
        finally:
            ctx.start_runs = start_runs
        ctx.tracker.remove_labels(number, ["ready"])
        ctx.tracker.add_labels(number, ["running"])
        ctx.store.start_run(number, branch=f"foragent/issue-{number}",
                            worktree=ctx.store.worktree_path(number))


@pytest.mark.parametrize("running_files", [None, []])
def test_lane_clone_requires_running_issue_files(ctx, running_files):
    ctx.config = replace(ctx.config, max_parallel=4)
    ctx.start_runs = True
    _issue(ctx, 1, running_files, running=True)
    _issue(ctx, 2, ["src/one.py"])
    _issue(ctx, 3, ["src/two.py"])
    tick(ctx)
    assert {run.number for run in ctx.store.active_runs()} == {1}
    assert all("ready" in ctx.tracker.get_issue(n).labels for n in (2, 3))


def test_lane_clone_checks_all_files_and_obeys_lane_pause(ctx):
    ctx.config = replace(ctx.config, max_parallel=4)
    ctx.start_runs = True
    _issue(ctx, 1, ["src/one.py", "src/shared*.py"], running=True)
    _issue(ctx, 2, ["src/two.py", "src/*shared.py"])
    _issue(ctx, 3, ["src/three.py"])
    until = NOW + timedelta(minutes=1)
    ctx.store.pause_lane("main", until)
    tick(ctx)
    assert {run.number for run in ctx.store.active_runs()} == {1}
    ctx.now = lambda: until
    tick(ctx)
    assert {run.number for run in ctx.store.active_runs()} == {1, 3}
    assert "ready" in ctx.tracker.get_issue(2).labels


def test_lane_clone_starts_with_backlog_on_later_ticks_and_stops_at_two(ctx):
    ctx.config = replace(ctx.config, max_parallel=4)
    ctx.start_runs = True
    starts = []
    ctx.launcher = lambda number: starts.append(number) or 7000 + number
    for number in range(1, 5):
        _issue(ctx, number, [f"src/file{number}.py"])
    tick(ctx)
    # Disjoint files fill the initial machine budget of two in one tick; no lane cap applies.
    assert starts == [1, 2]
    tick(ctx)
    tick(ctx)
    assert starts == [1, 2]
    assert {run.number for run in ctx.store.active_runs()} == {1, 2}


def _clone_backlog(ctx, running_files, candidate_files):
    ctx.config = replace(ctx.config, max_parallel=4)
    _issue(ctx, 1, running_files, running=True)
    for number in (2, 3):
        _issue(ctx, number, candidate_files)
    tick(ctx)  # Save the approved bodies before introducing concurrent changes.
    ctx.start_runs = True
    starts = []
    ctx.launcher = lambda number: starts.append(number) or 7000 + number
    return starts


def test_lane_clone_uses_running_snapshot_when_body_changes_after_integrity_check(ctx, monkeypatch):
    starts = _clone_backlog(ctx, ["src/shared.py"], ["src/shared.py"])
    approved_body = ctx.store.load_approval(1).body
    label_events = ctx.tracker.label_events

    def edit_after_running_check(number):
        if number == 2:
            ctx.tracker.edit_body(1, approved_body.replace("src/shared.py", "src/other.py"))
        return label_events(number)

    monkeypatch.setattr(ctx.tracker, "label_events", edit_after_running_check)
    tick(ctx)
    assert ctx.tracker.get_issue(1).body != approved_body
    assert ctx.store.load_approval(1).body == approved_body
    assert starts == []
    assert {run.number for run in ctx.store.active_runs()} == {1}
    assert all("ready" in ctx.tracker.get_issue(n).labels for n in (2, 3))


@pytest.mark.parametrize("approved_files,live_files,allowed", [
    (["src/shared.py"], ["src/other.py"], False),
    (["src/other.py"], ["src/shared.py"], True),
])
def test_lane_clone_candidate_files_come_from_snapshot(ctx, approved_files, live_files, allowed):
    _clone_backlog(ctx, ["src/shared.py"], approved_files)
    body = ctx.tracker.get_issue(2).body.replace(approved_files[0], live_files[0])
    ctx.tracker.edit_body(2, body)
    assert _can_start_in_lane(ctx, "main", ctx.tracker.get_issue(2)) is allowed


@pytest.mark.parametrize("missing_snapshot", [1, 2])
def test_lane_clone_requires_both_approval_snapshots(ctx, missing_snapshot):
    _clone_backlog(ctx, ["src/one.py"], ["src/two.py"])
    ctx.store.clear_approval(missing_snapshot)
    assert not _can_start_in_lane(ctx, "main", ctx.tracker.get_issue(2))


@pytest.mark.parametrize("legacy_run", [False, True])
@pytest.mark.parametrize("running_file,candidate_file,unchanged_body", [
    ("src/other.py", "src/shared.py", False),
    ("src/other.py", "src/unrelated.py", False),
    ("src/shared.py", "src/unrelated.py", True),
])
def test_lane_clone_defers_after_running_issue_reapproval(
    ctx, running_file, candidate_file, unchanged_body, legacy_run,
):
    ctx.config = replace(ctx.config, max_parallel=4)
    ctx.start_runs = True
    starts = []
    ctx.launcher = lambda number: starts.append(number) or 7000 + number
    _issue(ctx, 1, ["src/shared.py"])
    tick(ctx)
    original_approval = ctx.store.load_approval(1)
    assert starts == [1]
    if legacy_run:
        path = ctx.store.root / "runs/1.json"
        record = json.loads(path.read_text())
        del record["approval_body_sha"]
        path.write_text(json.dumps(record))
    for number in (2, 3):
        _issue(ctx, number, [candidate_file])

    ctx.now = lambda: NOW + timedelta(seconds=1)
    ctx.tracker.remove_labels(1, ["approved"])
    ctx.tracker.edit_body(1, original_approval.body.replace("src/shared.py", running_file))
    ctx.now = lambda: NOW + timedelta(seconds=2)
    ctx.tracker.record_label(1, "approved", "human")
    tick(ctx)

    allowed = unchanged_body and not legacy_run
    assert (ctx.store.load_approval(1).body_sha == original_approval.body_sha) is unchanged_body
    assert ctx.store.get_run(1).approval_body_sha == (None if legacy_run else original_approval.body_sha)
    assert starts == ([1, 2] if allowed else [1])
    assert {run.number for run in ctx.store.active_runs()} == ({1, 2} if allowed else {1})
    assert all("ready" in ctx.tracker.get_issue(n).labels for n in ((3,) if allowed else (2, 3)))


@pytest.mark.parametrize("started_at,approval_precedes_start", [
    (NOW - timedelta(microseconds=1), False),
    (NOW, True),
    (NOW + timedelta(microseconds=1), True),
    (NOW.astimezone(timezone(timedelta(hours=-9))), True),
    ((NOW - timedelta(seconds=1)).astimezone(timezone(timedelta(hours=-9))), False),
])
@pytest.mark.parametrize("candidate_file,overlap", [("src/one.py", True), ("src/two.py", False)])
def test_lane_clone_legacy_run_uses_approval_at_or_before_start(
    ctx, started_at, approval_precedes_start, candidate_file, overlap,
):
    starts = _clone_backlog(ctx, ["src/one.py"], [candidate_file])
    path = ctx.store.root / "runs/1.json"
    record = json.loads(path.read_text())
    del record["approval_body_sha"]
    record["started_at"] = started_at.isoformat()
    path.write_text(json.dumps(record))
    ctx.now = lambda: NOW + timedelta(seconds=2)
    tick(ctx)
    allowed = approval_precedes_start and not overlap
    assert starts == ([2] if allowed else [])
    assert {run.number for run in ctx.store.active_runs()} == ({1, 2} if allowed else {1})
    assert ctx.store.get_run(1).approval_body_sha is None


@pytest.mark.parametrize("left,right", [
    ("src/Straße.py", "src/STRASSE.py"),
    ("src/ß.py", "SRC/ss.PY"),
    ("Straße/module.py", "STRASSE/module.py"),
])
def test_literal_file_patterns_overlap_includes_full_casefold_aliases(left, right):
    assert _file_patterns_overlap(left, right, case_sensitive=False)
    assert _file_patterns_overlap(right, left, case_sensitive=False)
    assert not _file_patterns_overlap(left, right, case_sensitive=True)


def test_lane_clone_rejects_full_casefold_literal_aliases(ctx, monkeypatch):
    starts = _clone_backlog(ctx, ["src/Straße.py"], ["src/STRASSE.py"])
    monkeypatch.setattr("foragent.scheduler._filesystem_case_sensitive", lambda root: False)
    tick(ctx)
    assert starts == []
    assert {run.number for run in ctx.store.active_runs()} == {1}


def test_non_ascii_complementary_classes_keep_conservative_overlap():
    left, right = "src/[한-힣].py", "src/[!한-힣].py"
    assert _file_patterns_overlap(left, right, case_sensitive=False)
    assert _file_patterns_overlap(right, left, case_sensitive=False)
    assert not _file_patterns_overlap(left, right, case_sensitive=True)


def test_lane_clone_keeps_conservative_non_ascii_class_overlap(ctx, monkeypatch):
    starts = _clone_backlog(ctx, ["src/[한-힣].py"], ["src/[!한-힣].py"])
    monkeypatch.setattr("foragent.scheduler._filesystem_case_sensitive", lambda root: False)
    tick(ctx)
    assert starts == []
    assert {run.number for run in ctx.store.active_runs()} == {1}


@pytest.mark.parametrize("left,right", [
    ("src/Shared.py", "src/shared.py"),
    ("SRC/**/SHARED[0-9].PY", "src/pkg/shared7.py"),
    ("src/SHARED*.py", "SRC/*shared.py"),
])
def test_file_patterns_overlap_ignores_case_in_literals_and_globs(left, right):
    assert _file_patterns_overlap(left, right, case_sensitive=False)
    assert _file_patterns_overlap(right, left, case_sensitive=False)
    assert not _file_patterns_overlap(left, right, case_sensitive=True)


@pytest.mark.parametrize("left,right,expected", [
    ("src/?.py", "src/ß.py", True),
    ("src/?.py", "src/İ.py", True),
    ("src/??.py", "src/ß.py", False),
    ("src/ß.py", "src/ẞ.py", True),
    ("src/Straße.py", "SRC/STRASSE.PY", True),
    ("src/Stra?e.py", "src/STRASSE.py", False),
    ("src/[A-z].py", "src/_.py", True),
    ("src/[A-z].py", "src/[!a-z].py", True),
    ("src/[!A-z].py", "src/_.py", False),
    ("src/[Z-a].py", "src/_.py", True),
    ("src/[!Z-a].py", "src/_.py", False),
    ("src/[A-Z].py", "src/[a-z].py", True),
    ("src/[!A-Z].py", "src/[a-z].py", False),
    ("src/[À-Ö].py", "src/ô.py", True),
    ("src/[À-Ö].py", "src/×.py", False),
    # U+0180 (ƀ) and its uppercase U+0243 (Ƀ) lie inside both ranges' endpoints.
    ("src/[\u017e-\u0182].py", "src/[\u0240-\u0248].py", True),
])
def test_file_patterns_overlap_preserves_character_width_and_ranges(left, right, expected):
    assert _file_patterns_overlap(left, right, case_sensitive=False) is expected
    assert _file_patterns_overlap(right, left, case_sensitive=False) is expected


@pytest.mark.parametrize("running_file,candidate_file", [
    ("src/?.py", "src/ß.py"),
    ("src/[A-z].py", "src/_.py"),
    ("src/[Z-a].py", "src/_.py"),
])
def test_lane_clone_rejects_overlapping_character_globs(ctx, monkeypatch, running_file, candidate_file):
    starts = _clone_backlog(ctx, [running_file], [candidate_file])
    monkeypatch.setattr("foragent.scheduler._filesystem_case_sensitive", lambda root: False)
    tick(ctx)
    assert starts == []
    assert {run.number for run in ctx.store.active_runs()} == {1}
    assert all("ready" in ctx.tracker.get_issue(n).labels for n in (2, 3))


@pytest.mark.parametrize("case_sensitive", [True, False])
@pytest.mark.parametrize("failure", ["broken_symlink", "original_stat", "alternate_stat"])
def test_filesystem_case_probe_continues_after_entry_stat_failure(
    tmp_path, monkeypatch, case_sensitive, failure,
):
    first, healthy = tmp_path / "bench", tmp_path / "healthy"
    if failure == "broken_symlink":
        first.symlink_to(tmp_path / "missing")
    else:
        first.write_text("first")
    healthy.write_text("healthy")
    iterdir, stat = Path.iterdir, Path.stat
    visited = []

    def entries(path):
        return iter((first, healthy)) if path == tmp_path else iterdir(path)

    def filesystem_stat(path, *args, **kwargs):
        visited.append(path)
        if (failure == "original_stat" and path == first) or (
            failure == "alternate_stat" and path == first.with_name("BENCH")
        ):
            raise PermissionError(path)
        if path == healthy.with_name("HEALTHY"):
            if case_sensitive:
                raise FileNotFoundError(path)
            path = healthy
        return stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "iterdir", entries)
    monkeypatch.setattr(Path, "stat", filesystem_stat)
    assert _filesystem_case_sensitive(tmp_path) is case_sensitive
    assert healthy in visited


def test_lane_clone_starts_with_broken_first_root_symlink(ctx, monkeypatch):
    starts = _clone_backlog(ctx, ["src/one.py"], ["src/two.py"])
    root = ctx.repo_root
    broken = root / "bench"
    broken.symlink_to(root / "missing")
    iterdir = Path.iterdir

    def entries(path):
        if path == root:
            return iter([broken, *(entry for entry in iterdir(path) if entry != broken)])
        return iterdir(path)

    monkeypatch.setattr(Path, "iterdir", entries)
    tick(ctx)
    tick(ctx)
    assert starts == [2]
    assert {run.number for run in ctx.store.active_runs()} == {1, 2}


@pytest.mark.parametrize("case_sensitive", [True, False, None])
@pytest.mark.parametrize("running_file,candidate_file,overlap", [
    ("src/Shared.py", "src/shared.py", True),
    ("SRC/**/SHARED[0-9].PY", "src/pkg/shared7.py", True),
    ("src/Shared.py", "docs/other.py", False),
])
def test_lane_clone_uses_repository_filesystem_case_behavior(
    ctx, monkeypatch, case_sensitive, running_file, candidate_file, overlap,
):
    starts = _clone_backlog(ctx, [running_file], [candidate_file])
    root = ctx.repo_root
    alternate_names = {entry.name.swapcase(): entry for entry in root.iterdir()}
    stat = Path.stat

    def filesystem_stat(path, *args, **kwargs):
        if path.parent == root and path.name in alternate_names:
            if case_sensitive is None:
                raise PermissionError("Cannot determine filesystem case behavior")
            if case_sensitive:
                raise FileNotFoundError(path)
            path = alternate_names[path.name]
        return stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", filesystem_stat)
    tick(ctx)
    allowed = case_sensitive is not None and (case_sensitive or not overlap)
    assert starts == ([2] if allowed else [])
    assert {run.number for run in ctx.store.active_runs()} == ({1, 2} if allowed else {1})


def test_lane_clone_takes_a_machine_slot_like_any_other_run(ctx):
    from foragent.state import StateStore

    ctx.config = replace(ctx.config, max_parallel=4)
    ctx.start_runs = True
    starts = []
    ctx.launcher = lambda number: starts.append(number) or os.getpid()
    _issue(ctx, 1, ["src/one.py"], running=True)
    ctx.store.update_run(1, pid=os.getpid())
    _issue(ctx, 2, ["src/two.py"])
    _issue(ctx, 3, ["src/three.py"])
    # Another repository's live run fills the shared machine budget of two.
    sibling = StateStore(ctx.store.root.parent / "sibling", now=ctx.now)
    sibling.start_run(9, branch="foragent/issue-9", worktree=sibling.worktree_path(9))
    sibling.update_run(9, pid=os.getpid())
    tick(ctx)
    assert starts == []
    assert {run.number for run in ctx.store.active_runs()} == {1}
    sibling.finish_run(9, "merged")
    tick(ctx)
    assert starts == [2]
    assert {run.number for run in ctx.store.active_runs()} == {1, 2}
