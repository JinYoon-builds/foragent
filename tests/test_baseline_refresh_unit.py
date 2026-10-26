"""A moved-base baseline refresh measures the new base, not the PR checkout (#483)."""

from types import SimpleNamespace

import pytest

from conftest import git_command
from foragent import run_implementation
from foragent.config import Config
from foragent.git import GitRepo
from foragent.order import detect_test_commands
from foragent.reasons import Reason
from foragent.run_attempt import ExecutorStopped
from foragent.state import StateStore


def moved_base(git_history):
    """The checkout lacks pyproject.toml (as if the PR removed it); the new base has it."""
    git_history.update({"pyproject.toml": "[project]\nname = 'demo'\n",
                        "package.json": '{"scripts": {"test": "jest"}}\n'})
    git_command(git_history.clone, "fetch", "-q", "origin")
    assert detect_test_commands(git_history.clone) == []
    return GitRepo(git_history.clone)


def test_detection_reads_the_commit_files(git_history):
    git = moved_base(git_history)
    assert run_implementation._detect_test_command_at(git, "origin/main") == "uv run pytest"


def test_refresh_captures_with_the_new_base_command(git_history, tmp_path, monkeypatch):
    git = moved_base(git_history)
    seen = []
    monkeypatch.setattr(run_implementation, "_capture_acceptance_baseline",
                        lambda n, ctx, command, ref: seen.append((command, ref)) or "baseline")
    ctx = SimpleNamespace(config=Config(), store=StateStore(tmp_path / "state"),
                          monotonic=lambda: 0.0)
    ctx.store.update_run = lambda *args, **kwargs: None
    monkeypatch.setattr("foragent.ci_mode.enabled", lambda ctx, branch: False)

    tip = git_history.tip
    assert run_implementation.capture_acceptance_baseline(
        1, ctx, git, "main", tip, detect_at_ref=True) == "baseline"
    assert seen == [("uv run pytest", tip)]


def base_without_pyproject(git_history, tmp_path, monkeypatch):
    """The new base removes pyproject.toml; no test command is configured."""
    git_history.update({"pyproject.toml": None})
    git_command(git_history.clone, "fetch", "-q", "origin")
    flags = []
    monkeypatch.setattr(run_implementation, "flag", lambda ctx, n, reason, *args: flags.append(reason))
    monkeypatch.setattr("foragent.ci_mode.enabled", lambda ctx, branch: False)
    ctx = SimpleNamespace(config=Config(), store=StateStore(tmp_path / "state"), monotonic=lambda: 0.0,
                          repo_root=git_history.clone, git=GitRepo(git_history.clone))
    ctx.store.update_run = lambda *args, **kwargs: None
    return ctx, flags


def test_refresh_without_new_base_baseline_stops_as_327(git_history, tmp_path, monkeypatch):
    ctx, flags = base_without_pyproject(git_history, tmp_path, monkeypatch)
    with pytest.raises(ExecutorStopped) as stopped:
        run_implementation.capture_acceptance_baseline(
            1, ctx, ctx.git, "main", git_history.tip, detect_at_ref=True, required=True)
    assert stopped.value.outcome == Reason.ACCEPTANCE_BASELINE_UNAVAILABLE.value
    assert flags == [Reason.ACCEPTANCE_BASELINE_UNAVAILABLE]
    assert "acceptance baseline unavailable" in ctx.store.log_path(1).read_text()


def test_refresh_without_earlier_protection_keeps_todays_skip(git_history, tmp_path, monkeypatch):
    ctx, flags = base_without_pyproject(git_history, tmp_path, monkeypatch)
    assert run_implementation.capture_acceptance_baseline(
        1, ctx, ctx.git, "main", git_history.tip, detect_at_ref=True) is None
    assert flags == []


def following(git_history, monkeypatch, *, baseline_sha, baseline="old"):
    calls = []

    def capture(n, ctx, git, target_branch, ref, **kwargs):
        calls.append((ref, kwargs))
        return f"baseline@{ref}"

    monkeypatch.setattr(run_implementation, "capture_acceptance_baseline", capture)
    attempt = SimpleNamespace(n=1, ctx=None, git=GitRepo(git_history.clone), target_branch="main",
                              start_ref="origin/main", baseline=baseline, baseline_sha=baseline_sha)
    return attempt, calls


def test_follow_base_recaptures_once_for_any_newer_start_ref(git_history, monkeypatch):
    """Rebuilds and target switches move start_ref without passing refresh_base."""
    old = git_history.tip
    git_history.update({"src/new.py": "x = 1\n"})
    git_command(git_history.clone, "fetch", "-q", "origin")
    attempt, calls = following(git_history, monkeypatch, baseline_sha=old)

    run_implementation.follow_base(attempt)
    run_implementation.follow_base(attempt)

    new = git_history.tip
    assert calls == [(new, {"detect_at_ref": True, "required": True})]
    assert attempt.baseline == f"baseline@{new}" and attempt.baseline_sha == new


def test_follow_base_keeps_the_baseline_of_the_same_base(git_history, monkeypatch):
    git_command(git_history.clone, "fetch", "-q", "origin")
    attempt, calls = following(git_history, monkeypatch, baseline_sha=git_history.tip)
    run_implementation.follow_base(attempt)
    assert calls == [] and attempt.baseline == "old"


def test_follow_base_ignores_runs_without_a_baseline(git_history, monkeypatch):
    attempt, calls = following(git_history, monkeypatch, baseline_sha=None, baseline=None)
    run_implementation.follow_base(attempt)
    assert calls == []


def test_rebuild_verification_uses_the_rebuilt_base(git_history, monkeypatch):
    """_rebuild adopts the latest base; verification follows it before any check runs."""
    from foragent import run_checks, runner

    old = git_history.tip
    git_history.update({"src/new.py": "x = 1\n"})
    git_command(git_history.clone, "fetch", "-q", "origin")
    attempt, calls = following(git_history, monkeypatch, baseline_sha=old)
    attempt.start_ref = git_history.tip  # what _rebuild assigns

    class Stop(Exception):
        pass

    def capture(n, ctx, git, target_branch, ref, **kwargs):
        calls.append((ref, kwargs))
        raise Stop

    checks = []
    monkeypatch.setattr(run_implementation, "capture_acceptance_baseline", capture)
    monkeypatch.setattr("foragent.run_conflicts._prepare_target", lambda attempt: True)
    monkeypatch.setattr(run_checks, "_phase", lambda *args: None)
    monkeypatch.setattr(run_checks, "_progress", lambda *args: None)
    monkeypatch.setattr("foragent.ci_mode.stage_exclusions", lambda attempt: None)
    monkeypatch.setattr(runner, "_check", lambda *args, **kwargs: checks.append(args))
    attempt.git.stage_changes = lambda: None
    attempt.git.changed_paths = lambda ref: ["src/feature.py"]
    attempt.git.publication_paths = set()
    attempt.git.staged_tree_sha = lambda: "tree"
    attempt.worktree, attempt.log = git_history.clone, None
    attempt.ctx = SimpleNamespace(store=None)

    with pytest.raises(Stop):
        run_checks._verify_implementation(attempt)
    assert calls == [(git_history.tip, {"detect_at_ref": True, "required": True})]
    assert checks == []
