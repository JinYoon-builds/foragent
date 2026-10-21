"""Build provenance survives packaging and tolerates unavailable Git."""

import importlib.util
from pathlib import Path
import runpy
import subprocess
from types import SimpleNamespace

import pytest


@pytest.fixture
def builder(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "foragent_setup", Path(__file__).resolve().parents[1] / "setup.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / "src" / "foragent").mkdir(parents=True)
    return module, tmp_path, tmp_path / "src" / "foragent" / "_build.py"


def test_build_records_git_sha_for_worktree(builder, monkeypatch):
    module, root, target = builder
    (root / ".git").write_text("gitdir: /some/worktree\n")

    def git(args, **kwargs):
        assert args == ["git", "rev-parse", "--short", "HEAD"]
        assert kwargs["cwd"] == root
        return SimpleNamespace(stdout="a1b2c3d\n")

    monkeypatch.setattr(module.subprocess, "run", git)
    module.write_build_metadata(root)
    assert runpy.run_path(str(target))["GIT_SHA"] == "a1b2c3d"


@pytest.mark.parametrize("error", [
    FileNotFoundError("git"),
    subprocess.CalledProcessError(128, "git"),
    subprocess.TimeoutExpired("git", 5),
])
def test_git_failure_replaces_old_metadata_with_unknown(builder, monkeypatch, error):
    module, root, target = builder
    (root / ".git").mkdir()
    target.write_text("GIT_SHA = 'old'\n")

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(module.subprocess, "run", fail)
    module.write_build_metadata(root)
    assert runpy.run_path(str(target))["GIT_SHA"] == "unknown"


@pytest.mark.parametrize("saved_sha", [None, "a1b2c3d"])
def test_archive_build_preserves_sha_without_reading_parent_git(builder, monkeypatch, saved_sha):
    module, root, target = builder
    if saved_sha:
        target.write_text(f"GIT_SHA = {saved_sha!r}\n")

    def forbidden(*args, **kwargs):
        pytest.fail("an archive build must not read live Git")

    monkeypatch.setattr(module.subprocess, "run", forbidden)
    module.write_build_metadata(root)
    assert runpy.run_path(str(target))["GIT_SHA"] == (saved_sha or "unknown")
