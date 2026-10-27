"""A run's protected set follows the base it is built on, measured by the base (#483)."""

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


def following(git_history, monkeypatch, tmp_path, *, baseline_sha, baseline, result=None):
    """An attempt whose start_ref is origin/main; capture is recorded, not run."""
    calls, flags = [], []

    def capture(n, ctx, git, target_branch, ref, test_command=None):
        calls.append((ref, test_command))
        if isinstance(result, Exception):
            raise result
        return result if result is not None else SimpleNamespace(
            test_command=test_command, at=ref, node_ids=(PROTECTED,), excluded=(), collected_ids=None,
            failing_ids=None)

    monkeypatch.setattr(run_implementation, "capture_acceptance_baseline", capture)
    # #123's collection step: every protected id is still collected on the new base.
    from foragent import run_baseline
    monkeypatch.setattr(run_baseline, "collect_protected_ids", lambda n, ctx, baseline, sha: (set(), set()))
    monkeypatch.setattr(run_implementation, "flag", lambda ctx, n, reason, *args: flags.append(reason))
    ctx = SimpleNamespace(config=Config(), store=StateStore(tmp_path / "state"))
    attempt = SimpleNamespace(n=1, ctx=ctx, git=GitRepo(git_history.clone), target_branch="main",
                              start_ref="origin/main", baseline=baseline, baseline_sha=baseline_sha,
                              log=tmp_path / "run.log")
    return attempt, calls, flags


def protected(command="uv run pytest"):
    return SimpleNamespace(test_command=command, node_ids=(PROTECTED,))


PROTECTED = "tests/acceptance/test_a.py::test_a"


def test_detection_reads_the_commit_files(git_history):
    git = moved_base(git_history)
    assert run_implementation._detect_test_command_at(git, "origin/main") == "uv run pytest"


def test_refresh_captures_with_the_new_base_command(git_history, tmp_path, monkeypatch):
    """Without an earlier protected set the command comes from the new base, not the PR checkout."""
    old = git_history.tip
    moved_base(git_history)
    attempt, calls, _ = following(git_history, monkeypatch, tmp_path, baseline_sha=old, baseline=None)
    run_implementation.follow_base(attempt)
    assert calls == [(git_history.tip, "uv run pytest")]


def test_refresh_without_new_base_baseline_stops_as_327(git_history, tmp_path, monkeypatch):
    """The new base drops pyproject.toml: a run that had protection stops instead of losing it."""
    old = git_history.tip
    git_history.update({"pyproject.toml": None})
    git_command(git_history.clone, "fetch", "-q", "origin")
    attempt, _, flags = following(git_history, monkeypatch, tmp_path, baseline_sha=old, baseline=protected())
    attempt.ctx.store.log_path(1).parent.mkdir(parents=True, exist_ok=True)
    # _capture_acceptance_baseline returns None when the new base has nothing to measure.
    monkeypatch.setattr(run_implementation, "capture_acceptance_baseline", lambda *args: None)
    with pytest.raises(ExecutorStopped) as stopped:
        run_implementation.follow_base(attempt)
    assert stopped.value.outcome == Reason.ACCEPTANCE_BASELINE_UNAVAILABLE.value
    assert flags == [Reason.ACCEPTANCE_BASELINE_UNAVAILABLE]
    assert "acceptance baseline unavailable" in attempt.ctx.store.log_path(1).read_text()
    assert attempt.baseline_sha == old, "The stale protected set is never marked as the new base's"


def test_refresh_without_earlier_protection_keeps_todays_skip(git_history, tmp_path, monkeypatch):
    old = git_history.tip
    git_history.update({"src/new.py": "x = 1\n"})
    git_command(git_history.clone, "fetch", "-q", "origin")
    attempt, _, flags = following(git_history, monkeypatch, tmp_path, baseline_sha=old, baseline=None)
    monkeypatch.setattr(run_implementation, "capture_acceptance_baseline", lambda *args: None)
    run_implementation.follow_base(attempt)
    assert flags == [] and attempt.baseline is None and attempt.baseline_sha == git_history.tip


def test_follow_base_recaptures_once_for_any_newer_start_ref(git_history, tmp_path, monkeypatch):
    """Rebuilds and target switches move start_ref without passing refresh_base."""
    old = git_history.tip
    git_history.update({"src/new.py": "x = 1\n"})
    git_command(git_history.clone, "fetch", "-q", "origin")
    attempt, calls, _ = following(git_history, monkeypatch, tmp_path, baseline_sha=old,
                                  baseline=protected("pytest -q"))

    run_implementation.follow_base(attempt)
    run_implementation.follow_base(attempt)

    new = git_history.tip
    assert calls == [(new, "pytest -q")], "Keep the command the protected set was captured with"
    assert attempt.baseline.at == new and attempt.baseline_sha == new


def test_follow_base_keeps_the_baseline_of_the_same_base(git_history, tmp_path, monkeypatch):
    git_command(git_history.clone, "fetch", "-q", "origin")
    old = protected()
    attempt, calls, _ = following(git_history, monkeypatch, tmp_path, baseline_sha=git_history.tip, baseline=old)
    run_implementation.follow_base(attempt)
    assert calls == [] and attempt.baseline is old


def test_follow_base_ignores_runs_without_a_baseline(git_history, tmp_path, monkeypatch):
    attempt, calls, _ = following(git_history, monkeypatch, tmp_path, baseline_sha=None, baseline=None)
    run_implementation.follow_base(attempt)
    assert calls == []


def test_rebuild_verification_uses_the_rebuilt_base(git_history, tmp_path, monkeypatch):
    """_rebuild adopts the latest base; verification follows it before any check runs."""
    from foragent import run_checks, runner

    old = git_history.tip
    git_history.update({"src/new.py": "x = 1\n"})
    git_command(git_history.clone, "fetch", "-q", "origin")

    class Stop(Exception):
        pass

    attempt, calls, _ = following(git_history, monkeypatch, tmp_path, baseline_sha=old,
                                  baseline=protected(), result=Stop())
    attempt.start_ref = git_history.tip  # what _rebuild assigns
    checks = []
    monkeypatch.setattr("foragent.run_conflicts._prepare_target", lambda attempt: True)
    monkeypatch.setattr(run_checks, "_phase", lambda *args: None)
    monkeypatch.setattr(run_checks, "_progress", lambda *args: None)
    monkeypatch.setattr(runner, "_check", lambda *args, **kwargs: checks.append(args))
    attempt.git.stage_changes = lambda: None
    attempt.git.changed_paths = lambda ref: ["src/feature.py"]
    attempt.git.publication_paths = set()
    attempt.git.staged_tree_sha = lambda: "tree"
    attempt.worktree, attempt.log = git_history.clone, None
    attempt.parsed = SimpleNamespace(files=[])  # the harness guard reads ## Files

    with pytest.raises(Stop):
        run_checks._verify_implementation(attempt)
    assert calls == [(git_history.tip, "uv run pytest")]
    assert checks == []


def test_protection_lost_by_the_recapture_stays_empty_for_the_run(git_history, tmp_path, monkeypatch):
    # Every surviving id fails on the new base: the re-capture protects none (#123).
    from foragent.run_baseline import AcceptanceBaseline

    git = moved_base(git_history)
    old = git_command(git_history.clone, "rev-parse", "HEAD")
    node = "tests/acceptance/test_a.py::test_a"
    previous = AcceptanceBaseline("pytest", (node,), test_command="uv run pytest")
    empty = AcceptanceBaseline("pytest", (), collected_ids=(node,), failing_ids=(node,))
    attempt, calls, _ = following(git_history, monkeypatch, tmp_path, baseline_sha=old, baseline=previous,
                                  result=empty)
    attempt.git = git
    run_implementation.follow_base(attempt)
    assert attempt.baseline.node_ids == ()
    assert f"dropped protected node id {node}: failing on the new base" in attempt.log.read_text()
    # A later sync keeps it empty and does not re-capture.
    attempt.baseline_sha = old
    run_implementation.follow_base(attempt)
    assert len(calls) == 1 and attempt.baseline.node_ids == ()


def test_an_initially_empty_baseline_stays_empty_after_a_sync(git_history, tmp_path, monkeypatch):
    # Nothing was protected when the run started: a sync does not start protecting (#123).
    from foragent.run_baseline import AcceptanceBaseline

    old = git_command(git_history.clone, "rev-parse", "HEAD")
    moved_base(git_history)
    empty = AcceptanceBaseline("pytest", (), test_command="uv run pytest")
    attempt, calls, _ = following(git_history, monkeypatch, tmp_path, baseline_sha=old, baseline=empty)
    run_implementation.follow_base(attempt)
    assert calls == [] and attempt.baseline is empty and attempt.baseline_sha == git_history.tip


def test_a_failed_collection_stops_as_327_and_drops_nothing(git_history, tmp_path, monkeypatch):
    # A collection error on the new base: #483 still measures once, then the run stops (#123).
    from foragent import run_baseline
    from foragent.run_attempt import AttemptFailure

    old = git_history.tip
    moved_base(git_history)
    attempt, calls, flags = following(git_history, monkeypatch, tmp_path, baseline_sha=old, baseline=protected())
    attempt.ctx.store.log_path(1).parent.mkdir(parents=True, exist_ok=True)

    def broken(n, ctx, baseline, sha):
        raise AttemptFailure("acceptance baseline unavailable: collecting tests/acceptance on new failed")

    monkeypatch.setattr(run_baseline, "collect_protected_ids", broken)
    with pytest.raises(ExecutorStopped) as stopped:
        run_implementation.follow_base(attempt)
    assert stopped.value.outcome == Reason.ACCEPTANCE_BASELINE_UNAVAILABLE.value
    assert flags == [Reason.ACCEPTANCE_BASELINE_UNAVAILABLE] and len(calls) == 1
    assert attempt.baseline.node_ids == (PROTECTED,) and attempt.baseline_sha == old
    assert "dropped" not in (attempt.log.read_text() if attempt.log.exists() else "")
