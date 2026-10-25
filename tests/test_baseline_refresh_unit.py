"""A moved-base baseline refresh measures the new base, not the PR checkout (#483)."""

from types import SimpleNamespace

from conftest import git_command
from foragent import run_implementation
from foragent.config import Config
from foragent.git import GitRepo
from foragent.order import detect_test_commands
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
