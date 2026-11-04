"""Role memory reaches the existing Git publication boundary without a reminder."""

from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from conftest import git_command
from foragent.config import Config
from foragent.executors import ExecResult
from foragent.git import GitRepo
from foragent.orch_architect import run_session
from foragent.scheduler import TickContext, tick
from foragent.state import StateStore
from foragent.tracker import FakeTracker


NOW = datetime(2026, 11, 2, 12, tzinfo=UTC)
DIRECTORIES = {name: f".claude/agent-memory/fa-{name}" for name in
               ("orchestrator", "architect", "docs")}
DIRECTORIES["quality"] = ".claude/agent-memory/review-team"


@pytest.fixture
def publication(git_history, tmp_path, monkeypatch):
    """Real Git objects; forbidden Git commands are replaced at the adapter boundary.

    Fast-import implements a path-only commit. A local bare fetch implements delivery.
    The product still owns path selection, scheduling, staging and retry decisions.
    """
    roles = {name: {"agent": f"fa-{name}", "skills": [], "memory": folder + "/"}
             for name, folder in DIRECTORIES.items()}
    config = Config(repo="owner/repo", language="en", skill_libraries=[], roles=roles)
    original = {f"{folder}/MEMORY.md": f"Old {name} lesson.\n"
                for name, folder in DIRECTORIES.items()}
    original[".gitignore"] = "**/new-lesson.md\n"
    git_history.update(original)
    root = git_history.clone
    git_command(root, "fetch", "origin", "main")
    git_command(root, "merge", "--ff-only", "origin/main")
    failures = []

    def transport(args):
        directory = root
        while args[:1] == ["-C"]:
            directory, args = Path(args[1]), args[2:]
        if "commit" in args:
            paths = (args[args.index("--") + 1:] if "--only" in args
                     else git_command(directory, "diff", "--cached", "--name-only").splitlines())
            paths = [path for path in paths if not path.startswith(":!")]
            parent = git_command(directory, "rev-parse", "HEAD")
            branch = git_command(directory, "symbolic-ref", "HEAD")
            staged = git_command(directory, "ls-files", "--stage", "-z", "--", *paths)
            stream = [f"commit {branch}\n", "committer Tests <tests@example.com> 1793620800 +0000\n",
                      "data 11\nSave memory\n", f"from {parent}\n"]
            for path in paths:
                stream.append(f"D {json.dumps(path)}\n")
            for entry in filter(None, staged.split("\0")):
                metadata, path = entry.split("\t", 1)
                mode, sha, stage = metadata.split()
                assert stage == "0"
                stream.append(f"M {mode} {sha} {json.dumps(path)}\n")
            git_command(directory, "fast-import", "--quiet", input="".join(stream) + "\ndone\n")
            return ""
        if args[:1] == ["push"]:
            if failures:
                failures.pop()
                raise subprocess.CalledProcessError(1, ["git", "push"], stderr="offline")
            ref = args[-1]
            source, destination = ref.split(":") if ":" in ref else (f"refs/heads/{ref}",) * 2
            return git_command(git_history.origin, "fetch", str(directory), f"{source}:{destination}")
        assert not {"reset", "checkout", "stash"}.intersection(args)
        return subprocess.run(["git", *args], cwd=directory, check=True,
                              capture_output=True, text=True).stdout

    store = StateStore(tmp_path / "state", now=lambda: NOW)
    context = TickContext(repo=config.repo, repo_root=root, tracker=FakeTracker(strict=True),
                          store=store, config=config, git=GitRepo(root, transport),
                          now=lambda: NOW, launcher=lambda *a, **kw: pytest.fail("No lane job is due"),
                          start_runs=False)
    monkeypatch.setattr("foragent.scheduler.machine_load", lambda: ("normal", 0.0))
    return SimpleNamespace(root=root, origin=git_history.origin, config=config, store=store,
                           context=context, failures=failures)


def session_changes(case, monkeypatch, role):
    folder = DIRECTORIES[role]
    expected = {f"{folder}/MEMORY.md": f"Old {role} lesson.\nKeep the selected locale.\n".encode(),
                f"{folder}/new-lesson.md": b"A newly learned durable fact.\n"}

    def execute(**kwargs):
        for name, content in expected.items():
            (kwargs["worktree"] / name).write_bytes(content)
        return ExecResult(0, False, "Lessons recorded.", kwargs["log_path"])

    monkeypatch.setattr("foragent.executors.executor_for", lambda _: SimpleNamespace(run=execute))
    (case.store.root / "logs").mkdir(parents=True)
    assert run_session(case.root, case.store, case.config, "Completed order", NOW.isoformat(),
                       lambda: NOW, role=role) == 0
    return expected


def recorded_ref(repository, expected):
    # A published base commit and a published PR head both satisfy the contract.
    refs = git_command(repository, "for-each-ref", "--format=%(refname)", "refs/heads").splitlines()
    for ref in refs:
        results = [subprocess.run(["git", "-C", str(repository), "show", f"{ref}:{name}"],
                                  capture_output=True) for name in expected]
        if all(result.returncode == 0 and result.stdout == content
               for result, content in zip(results, expected.values())):
            return ref
    pytest.fail(f"Role memory has not reached committed Git history: {list(expected)}")


@pytest.mark.parametrize("role", list(DIRECTORIES))
def test_role_session_memory_reaches_published_history(publication, monkeypatch, role):
    case = publication
    expected = session_changes(case, monkeypatch, role)
    unrelated = case.root / "unrelated.txt"
    unrelated.write_text("A different session's staged work.\n")
    git_command(case.root, "add", "unrelated.txt")
    tick(case.context)
    ref = recorded_ref(case.origin, expected)
    assert "unrelated.txt" not in git_command(case.origin, "ls-tree", "-r", "--name-only", ref).splitlines()
    assert "unrelated.txt" in git_command(case.root, "diff", "--cached", "--name-only").splitlines()
    before = git_command(case.origin, "show-ref", "--heads")
    tick(case.context)
    assert git_command(case.origin, "show-ref", "--heads") == before, "An idle cycle must not publish twice"


def test_checkout_cleanup_and_fresh_clone_retain_role_memory(publication, monkeypatch, tmp_path):
    case = publication
    expected = session_changes(case, monkeypatch, "docs")
    tick(case.context)
    # Destructive operations affect only this disposable fixture checkout.
    git_command(case.root, "clean", "-fdx", "--", DIRECTORIES["docs"])
    git_command(case.root, "restore", "--source=HEAD", "--worktree", "--", DIRECTORIES["docs"])
    for name, content in expected.items():
        assert (case.root / name).read_bytes() == content
    ref = recorded_ref(case.origin, expected)
    clone = tmp_path / "new-machine"
    git_command(tmp_path, "clone", "--branch", ref.removeprefix("refs/heads/"), str(case.origin), str(clone))
    for name, content in expected.items():
        assert (clone / name).read_bytes() == content


def test_delivery_failure_keeps_role_memory_committed_and_retries(publication, monkeypatch):
    case = publication
    expected = session_changes(case, monkeypatch, "architect")
    case.failures.append("offline")
    tick(case.context)
    recorded_ref(case.root, expected)
    assert not case.failures, "The ordinary cycle must attempt delivery"
    with pytest.raises(pytest.fail.Exception, match="has not reached committed Git history"):
        recorded_ref(case.origin, expected)
    git_command(case.root, "clean", "-fdx", "--", DIRECTORIES["architect"])
    git_command(case.root, "restore", "--source=HEAD", "--worktree", "--", DIRECTORIES["architect"])
    for name, content in expected.items():
        assert (case.root / name).read_bytes() == content
    # A later gate context must recover delivery from durable state, not an in-memory flag.
    case.context.store = StateStore(case.store.root, now=lambda: NOW)
    tick(case.context)
    recorded_ref(case.origin, expected)
