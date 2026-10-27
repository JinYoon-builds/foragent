"""Package-tree verification and nonfatal post-merge recovery boundaries."""

import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from conftest import BASE_TIME, git_command
from foragent import orch_reinstall, tool_install
from foragent.git import GitError, GitRepo
from foragent.state import StateStore


@pytest.fixture
def reinstall(git_history, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    history = git_history
    history.update({
        "pyproject.toml": '[project]\nname = "foragent"\n',
        "src/foragent/__init__.py": "VERSION = 'merged'\n",
        "src/foragent/data/settings.json": '{"enabled": true}\n',
    })
    ctx = SimpleNamespace(git=GitRepo(history.clone), store=StateStore(tmp_path / "state"),
                          now=lambda: BASE_TIME)
    calls = []
    case = SimpleNamespace(ctx=ctx, history=history, calls=calls)
    bin_dir = tmp_path / "bin"

    def uv(args, *, cwd, env=None):
        calls.append((args, cwd))
        if args[:2] == ["tool", "install"]:
            assert args[2] == "--force" and "--reinstall" not in args
            case.package = Path(env["UV_TOOL_DIR"]) / "foragent/lib/python3.12/site-packages/foragent"
            # Like setup.py, the build writes metadata into the source tree.
            source = Path(args[-1]) / "src/foragent"
            (source / "_build.py").write_text(f"GIT_SHA = '{history.tip}'\n")
            shutil.copytree(source, case.package)
            for name in tool_install.ENTRY_POINTS:
                shim = Path(env["UV_TOOL_BIN_DIR"]) / name
                shim.parent.mkdir(parents=True, exist_ok=True)
                shim.write_text("installed")
        else:
            assert args == ["tool", "dir", "--bin"]
        return str(bin_dir)

    monkeypatch.setattr(orch_reinstall, "_uv", uv)
    monkeypatch.setattr(tool_install.psutil, "process_iter", lambda *a, **kw: iter(()))
    case.uv = uv
    before = git_command(history.clone, "worktree", "list", "--porcelain")
    yield case
    assert git_command(history.clone, "worktree", "list", "--porcelain") == before
    assert not (history.clone / "pyproject.toml").exists()
    for _, worktree in calls:
        assert not worktree.exists()


def failures(case):
    path = case.ctx.store.root / "orch-events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_build_metadata_and_python_caches_match_merged_base(reinstall, monkeypatch, capsys):
    c = reinstall

    def uv(args, *, cwd, env=None):
        result = c.uv(args, cwd=cwd, env=env)
        if args[:2] == ["tool", "install"]:
            cache = c.package / "__pycache__"
            cache.mkdir()
            (cache / "__init__.cpython-312.pyc").write_bytes(b"cache")
        return result

    monkeypatch.setattr(orch_reinstall, "_uv", uv)
    orch_reinstall.after_merge(c.ctx, 42, "main")
    output = capsys.readouterr().out
    assert "reinstalled" in output and "matches" in output and c.history.tip in output
    assert (c.package / "_build.py").read_text() == f"GIT_SHA = '{c.history.tip}'\n"
    assert not failures(c)


@pytest.mark.parametrize("change", ["changed", "missing", "extra", "build-metadata"])
def test_identical_init_does_not_hide_package_differences(reinstall, monkeypatch, capsys, change):
    c = reinstall
    filename = "data/settings.json" if change in {"changed", "missing"} else (
        "data/stale.json" if change == "extra" else "_build.py")

    def uv(args, *, cwd, env=None):
        result = c.uv(args, cwd=cwd, env=env)
        if args[:2] == ["tool", "install"]:
            path = c.package / filename
            if change == "missing":
                path.unlink()
            else:
                path.write_text("different\n")
        return result

    monkeypatch.setattr(orch_reinstall, "_uv", uv)
    orch_reinstall.after_merge(c.ctx, 42, "main")
    assert f"reinstall failed: installed package differs from merged source: {filename}" in capsys.readouterr().out
    event, = failures(c)
    assert event["kind"] == "reinstall-failed" and "PR #42" in event["summary"]
    assert filename in event["summary"]


@pytest.mark.parametrize("layout", ["missing", "ambiguous", "empty-tool-dir"])
def test_installed_package_must_be_found_unambiguously(reinstall, monkeypatch, capsys, layout):
    c = reinstall

    def uv(args, *, cwd, env=None):
        result = c.uv(args, cwd=cwd, env=env)
        if layout == "empty-tool-dir" and args == ["tool", "dir", "--bin"]:
            return ""
        if args[:2] == ["tool", "install"]:
            if layout == "missing":
                shutil.rmtree(c.package)
            elif layout == "ambiguous":
                shutil.copytree(c.package, c.package.parents[2] / "python3.13/site-packages/foragent")
        return result

    monkeypatch.setattr(orch_reinstall, "_uv", uv)
    orch_reinstall.after_merge(c.ctx, 42, "main")
    assert "reinstall failed:" in capsys.readouterr().out
    assert failures(c)[0]["kind"] == "reinstall-failed"


@pytest.mark.parametrize("stage", ["fetch", "worktree", "installer", "timeout"])
def test_post_merge_errors_are_reported_and_temporary_sources_cleaned(reinstall, monkeypatch, capsys, stage):
    c = reinstall

    def failed(*args, **kwargs):
        if stage == "worktree":
            # Git may create a directory before failing to register a worktree.
            args[0].mkdir()
        if stage == "timeout":
            raise subprocess.TimeoutExpired(["uv", "tool", "install"], 300)
        if stage == "installer":
            raise FileNotFoundError("uv is unavailable")
        raise GitError("cannot access merged source")

    if stage in {"fetch", "worktree"}:
        monkeypatch.setattr(c.ctx.git, "fetch" if stage == "fetch" else "add_detached_worktree", failed)
    else:
        monkeypatch.setattr(orch_reinstall, "_uv", failed)
    orch_reinstall.after_merge(c.ctx, 42, "main")
    output = capsys.readouterr().out
    assert len(output.splitlines()) == 1 and output.startswith("reinstall failed:")
    assert failures(c)[0]["kind"] == "reinstall-failed"
