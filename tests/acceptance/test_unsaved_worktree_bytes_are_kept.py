"""Acceptance test for unsaved-worktree-bytes-are-kept. Locked by Foragent; do not edit.

When a stopped run's WIP save fails (a stale index lock left by a killed git, or a
commit that cannot be made), the issue worktree is still removed. Before it goes,
every unsaved, non-ignored file is copied as bytes into a backup folder under the
state root, and one worktree-kept event names that folder and the file count.
"""

import re
import shutil
import subprocess

import pytest

from conftest import git_command
from test_orch_stop import _case, _invoke
from test_stop_whole_tree import events, fake_processes  # noqa: F401 - fixture

MODIFIED = "existing.txt", b"Half-done edit that no commit holds \xea\xb0\x80\n"
UNTRACKED = "draft/brand-new.py", b"def unfinished():\n    return 'only on disk'\n"
IGNORED = "cache/build.bin", b"\x00ignored cache bytes that must not be copied\x00"
COMMITTED = "committed.txt", b"Already saved in a commit on the issue branch.\n"
UNCHANGED = b"Keep this exactly.\n"  # untouched.txt, tracked and clean


def _copies(case, content):
    """Files under the state root, outside the live worktrees, holding exactly these bytes."""
    root = case.store.root
    worktrees = root / "worktrees"
    return [path for path in root.rglob("*")
            if path.is_file() and worktrees not in path.parents and path.read_bytes() == content]


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _kept(case):
    return [event for event in events(case) if event["kind"] == "worktree-kept" and event.get("issue") == 1]


def _break_save(case, monkeypatch, worktree, trigger):
    if trigger == "stale-index-lock":
        # A git process killed mid-write leaves its lock behind; every index write then fails.
        lock = git_command(worktree, "rev-parse", "--path-format=absolute", "--git-path", "index.lock")
        open(lock, "w").close()
        return
    original = case.git._run

    def unsigned(args):
        if "commit" in args:  # Signing unavailable: every commit attempt fails.
            raise subprocess.CalledProcessError(128, ["git", *args], stderr="error: gpg failed to sign the data")
        return original(args)

    monkeypatch.setattr(case.git, "_run", unsigned)


def _rerun_clean(case, table):
    """Put issue 1 back to running on a fresh, clean worktree with a new fake process tree."""
    worktree = case.store.worktree_path(1)
    case.git.add_worktree(worktree, "foragent/issue-1-clean", "origin/main")
    start = case.clock[0].timestamp()
    root = table.add(46000, cwd=worktree, start_time=start,
                     argv=("fa", "gate", "run-issue", "1"), marker="owner/repo#1")
    case.store.start_run(1, branch="foragent/issue-1-clean", worktree=worktree, pid=root.pid)
    case.tracker.add_labels(1, ["running"])
    case.tracker.remove_labels(1, ["ready"])
    return worktree


@pytest.mark.parametrize("trigger", ["stale-index-lock", "commit-fails"])
def test_failed_wip_save_keeps_unsaved_bytes_before_worktree_removal(
    git_history, tmp_path, monkeypatch, fake_processes, capsys, trigger,  # noqa: F811
):
    git_history.update({".gitignore": "cache/\n"})
    case, tree, main = _case(git_history, tmp_path, monkeypatch, fake_processes)
    worktree = case.store.worktree_path(1)
    # A file committed on the issue branch is saved already; it is not a backup's job.
    (worktree / COMMITTED[0]).write_bytes(COMMITTED[1])
    git_command(worktree, "add", "-A")
    case.git.at(worktree).commit("Saved before the stop")  # The case's offline commit transport.
    shutil.rmtree(worktree / ".foragent")  # Runner scratch from the shared setup; not this test's subject.
    assert not git_command(worktree, "status", "--porcelain"), "setup: the worktree must start clean"
    for name, content in (MODIFIED, UNTRACKED, IGNORED):
        (worktree / name).parent.mkdir(parents=True, exist_ok=True)
        (worktree / name).write_bytes(content)
    assert git_command(worktree, "check-ignore", IGNORED[0]) == IGNORED[0]
    _break_save(case, monkeypatch, worktree, trigger)

    code, output = _invoke(main, ["1", "--keep-work", "--why", "Reprioritize the lane"], capsys)

    assert code == 0, output
    assert not worktree.exists(), "The stop must still remove the worktree; nothing waits on a person"
    run = case.store.get_run(1)
    assert (run.status, run.outcome) == ("finished", "stopped"), run
    assert any(event["kind"] == "wip-preservation-failed" and event.get("issue") == 1 for event in events(case)), (
        f"The {trigger} trigger did not make the WIP save fail; the test setup is wrong")
    modified, untracked = _copies(case, MODIFIED[1]), _copies(case, UNTRACKED[1])
    assert len(modified) == 1, f"Unsaved edit to {MODIFIED[0]} was lost or copied {len(modified)} times"
    assert len(untracked) == 1, f"Untracked {UNTRACKED[0]} was lost or copied {len(untracked)} times"
    assert not _copies(case, IGNORED[1]), "An ignored cache file was copied into the backup"
    assert not _copies(case, COMMITTED[1]), "A file already saved in a commit was copied into the backup"

    kept = _kept(case)
    assert len(kept) == 1, f"Expected one worktree-kept event for the backup, got {kept}"
    folders = {path for text in _strings(kept[0]) for path in map(case.store.root.joinpath, [text])
               if path.is_dir() and case.store.root in path.parents}
    folder = next((path for path in folders if path in modified[0].parents and path in untracked[0].parents), None)
    assert folder is not None, f"worktree-kept must name the backup folder holding both files: {kept[0]}"
    counts = [value for value in kept[0].values() if isinstance(value, int) and not isinstance(value, bool)]
    summary = " ".join(_strings(kept[0])).replace(str(folder), "")
    assert 2 in counts or re.search(r"(?<![\d.:-])2(?![\d.:-])", summary), (
        f"worktree-kept must give the file count 2: {kept[0]}")
    assert not _copies(case, UNCHANGED), "A clean tracked file was copied into the backup"

    # A clean worktree removed the same way leaves no backup and no new event.
    clean = _rerun_clean(case, fake_processes)
    code, output = _invoke(main, ["1", "--keep-work", "--why", "Stop the clean rerun"], capsys)
    assert code == 0, output
    assert not clean.exists()
    assert len(_kept(case)) == 1, "A clean worktree must not add a worktree-kept event"
    assert not _copies(case, UNCHANGED), "A clean worktree's files were copied into a backup"
    assert len(_copies(case, MODIFIED[1])) == 1, "The earlier backup must stay as it was"
