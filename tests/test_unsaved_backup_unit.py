"""Boundary checks for the backup a failed WIP save leaves before a scheduler or runner worktree removal."""

import json
import subprocess
from datetime import UTC, timedelta
from pathlib import Path

import pytest

from foragent.git import GitError, GitRepo
from foragent.runner import run_issue
from foragent.scheduler import TickContext, TickReport, _kill_run

from conftest import git_command
from runner_support import run_case  # noqa: F401 - fixture


def _kill(c, monkeypatch, failure, content=b"partial implementation\n"):
    path = c.store.worktree_path(1)
    c.ctx.git.add_worktree(path, "foragent/issue-1", "origin/main")
    (path / "unfinished.txt").write_bytes(content)
    git_run = c.ctx.git._run

    def unavailable(args):
        command = args
        while command[:1] == ["-C"]:
            command = command[2:]
        if (failure == "stage" and command[:2] == ["ls-files", "--modified"]) or (
                failure == "push" and command[:1] == ["push"]):
            raise subprocess.CalledProcessError(1, ["git", *args], stderr=f"{failure} unavailable")
        return git_run(args)

    monkeypatch.setattr(c.ctx.git, "_run", unavailable)
    ctx = TickContext(
        repo=c.ctx.repo, repo_root=c.ctx.repo_root, tracker=c.tracker, store=c.store,
        config=c.ctx.config, git=c.ctx.git, now=c.ctx.now, launcher=lambda n: None,
        runner_process_exists=lambda n: False,
    )
    report = TickReport()
    _kill_run(ctx, 1, report)
    assert not path.exists()
    c.tracker.remove_labels(1, ["running"])
    return report


def _kept(c):
    lines = (c.store.root / "orch-events.jsonl").read_text().splitlines()
    return [event for event in map(json.loads, lines) if event["kind"] == "worktree-kept"]


def test_failed_save_backs_up_once_per_removal_in_a_new_folder(run_case, monkeypatch):  # noqa: F811
    c = run_case
    _kill(c, monkeypatch, "stage")
    c.now[0] += timedelta(seconds=1)  # A second run needs its own start time.
    c.store.start_run(1, branch="foragent/issue-1", worktree=c.store.worktree_path(1))
    first = Path(_kept(c)[0]["path"])
    taken = first.with_name(f"issue-1-{c.now[0].astimezone(UTC):%Y%m%dT%H%M%S%fZ}")
    earlier = [taken, *(taken.with_name(f"{taken.name}~{k}") for k in range(1, 1000))]
    for folder in earlier:  # Earlier backups hold this second's name and every old suffix limit.
        folder.mkdir()
        (folder / "unfinished.txt").write_bytes(b"older backup\n")
    _kill(c, monkeypatch, "stage", b"second attempt\n")
    kept = _kept(c)
    assert all((folder / "unfinished.txt").read_bytes() == b"older backup\n" for folder in earlier)
    assert Path(kept[1]["path"]) == taken.with_name(f"{taken.name}~1000")
    assert [event["files"] for event in kept] == [1, 1] and kept[0]["kept"] == ["unfinished.txt"]
    folders = [event["path"] for event in kept]
    assert len(set(folders)) == 2
    contents = sorted(open(f"{folder}/unfinished.txt", "rb").read() for folder in folders)
    assert contents == [b"partial implementation\n", b"second attempt\n"]
    assert "Unsaved work copied to" in c.store.log_path(1).read_text()


def test_saved_commit_with_failed_push_copies_nothing(run_case, monkeypatch):  # noqa: F811
    c = run_case
    _kill(c, monkeypatch, "push")
    assert _kept(c) == [] and not (c.store.root / "backups").exists()


def test_copy_failure_is_logged_and_the_worktree_still_goes(run_case, monkeypatch):  # noqa: F811
    c = run_case

    def broken(self):
        raise GitError("disk full")

    monkeypatch.setattr(GitRepo, "unsaved_files", broken)
    report = _kill(c, monkeypatch, "stage")
    assert _kept(c) == []
    assert "Copying unsaved files before worktree removal failed: disk full" in c.store.log_path(1).read_text()
    assert any("WIP preservation failed" in action for action in report.actions)


def test_runner_failed_save_keeps_unsaved_bytes_before_its_own_removal(run_case, monkeypatch):  # noqa: F811
    c = run_case
    scripted = c.executor.run

    def leave_lock(**kwargs):
        result = scripted(**kwargs)
        lock = git_command(kwargs["worktree"], "rev-parse", "--path-format=absolute", "--git-path", "index.lock")
        open(lock, "w").close()  # A git process killed mid-write: every later index write fails.
        return result

    c.executor.steps = [{".foragent/QUESTION.md": "Which behavior is intended?", "impl/new.py": "only on disk\n"}]
    monkeypatch.setattr(c.executor, "run", leave_lock)
    run_issue(1, c.ctx)
    assert not c.store.worktree_path(1).exists()
    kept = _kept(c)
    assert len(kept) == 1 and kept[0]["kept"] == ["impl/new.py"] and kept[0]["files"] == 1
    assert (Path(kept[0]["path"]) / "impl" / "new.py").read_bytes() == b"only on disk\n"


def test_project_stop_keeps_the_worktree_so_no_copy_is_made(run_case, monkeypatch):  # noqa: F811
    from foragent import orch_stop
    from foragent.run_publish_preserve import _preserve_failed_implementation

    c = run_case
    path = c.store.worktree_path(1)
    c.ctx.git.add_worktree(path, "foragent/issue-1", "origin/main")
    (path / "unfinished.txt").write_bytes(b"partial implementation\n")
    lock = git_command(path, "rev-parse", "--path-format=absolute", "--git-path", "index.lock")
    open(lock, "w").close()
    with monkeypatch.context() as stop:
        stop.setattr(orch_stop, "active", lambda store: "Project stop")
        with pytest.raises(GitError):
            _preserve_failed_implementation(1, c.ctx, c.ctx.git.at(path), "stopped",
                                            approval=c.store.load_approval(1))
    assert not (c.store.root / "backups").exists()
    Path(lock).unlink()
    c.ctx.git.remove_worktree(path)
    c.store.finish_run(1, "error")
    c.tracker.remove_labels(1, ["running"])
