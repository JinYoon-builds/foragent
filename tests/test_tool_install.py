"""Versioned tool installs use only temporary directories and a fake uv/process table."""

import fcntl
import os
from pathlib import Path
import plistlib
import subprocess
from types import SimpleNamespace

import pytest

from foragent import capacity, launchd, orch_reinstall, tool_install, update_check


def make_version(path, content="old"):
    interpreter = path / "tools/foragent/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("python")
    for name in tool_install.ENTRY_POINTS:
        executable = interpreter.with_name(name)
        executable.write_text(content)
        shim = path / "bin" / name
        shim.parent.mkdir(parents=True, exist_ok=True)
        shim.symlink_to(executable)
    return path


def assert_locked(machine):
    with (machine / "install.lock").open("a") as stream:
        with pytest.raises(BlockingIOError):
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def versions(root):
    return {path for path in root.iterdir() if not path.is_symlink()}


@pytest.fixture
def installation(tmp_path, monkeypatch):
    machine = tmp_path / "machine"
    root = machine / "tool-versions"
    root.mkdir(parents=True)
    live_bin = tmp_path / "live-bin"
    live_bin.mkdir()
    old = make_version(tmp_path / "original-uv")
    for name in tool_install.ENTRY_POINTS:
        (live_bin / name).symlink_to(old / "bin" / name)
    case = SimpleNamespace(machine=machine, root=root, bin=live_bin, old=old,
                           calls=[], versions=[], spawned=[], install_error=None, missing=None)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(capacity, "machine_root", lambda: machine)
    monkeypatch.setattr(tool_install.sys, "platform", "linux")
    monkeypatch.setattr(tool_install.psutil, "process_iter", lambda *a, **kw: iter(()))

    def uv(argv, **kwargs):
        assert argv[0] == "uv"
        assert_locked(machine)
        case.calls.append((argv, kwargs))
        if argv[1:] == ["tool", "dir", "--bin"]:
            return subprocess.CompletedProcess(argv, 0, stdout=str(live_bin), stderr="")
        assert argv[1:4] == ["tool", "install", "--force"]
        assert "--reinstall" not in argv
        env = kwargs["env"]
        version = Path(env["UV_TOOL_DIR"]).parent
        assert version.parent == root
        assert env["UV_TOOL_BIN_DIR"] == str(version / "bin")
        assert version not in case.versions
        case.versions.append(version)
        make_version(version, "new")
        if case.missing:
            (version / "bin" / case.missing).unlink()
        if case.install_error:
            raise case.install_error
        return subprocess.CompletedProcess(argv, 0, stdout="installed", stderr="")

    monkeypatch.setattr(orch_reinstall.subprocess, "run", uv)
    monkeypatch.setattr(launchd.subprocess, "Popen", lambda argv, **kw: case.spawned.append((argv, kw)))
    return case


@pytest.mark.parametrize("managed", [False, True], ids=["original-uv", "versioned"])
@pytest.mark.parametrize("shim_layout", ["symlink", "regular-file"])
def test_install_verifies_then_atomically_replaces_both_shims_without_a_gap(
    installation, tmp_path, monkeypatch, managed, shim_layout,
):
    c = installation
    if managed:
        c.old = make_version(c.root / "20000101-old")
        (c.root / "current").symlink_to(c.old)
        for name in tool_install.ENTRY_POINTS:
            (c.bin / name).unlink()
            (c.bin / name).symlink_to(c.root / "current/bin" / name)
    elif shim_layout == "regular-file":
        for name in tool_install.ENTRY_POINTS:
            (c.bin / name).unlink()
            (c.bin / name).write_text("old")
    old_files = {p.relative_to(c.old): p.read_bytes() for p in c.old.rglob("*") if p.is_file()}
    events = []

    def observe(event):
        assert_locked(c.machine)
        assert all((c.bin / name).exists() for name in tool_install.ENTRY_POINTS)
        assert {(c.bin / name).read_text() for name in tool_install.ENTRY_POINTS} in ({"old"}, {"new"})
        events.append(event)

    symlink_to, replace = Path.symlink_to, os.replace

    def link(path, target, **kwargs):
        symlink_to(path, target, **kwargs)
        observe("link")

    def publish(source, target):
        assert "verified" in events
        assert source.parent == target.parent in {c.bin, c.root}
        assert source.is_symlink()
        replace(source, target)
        observe(target.name)

    monkeypatch.setattr(Path, "symlink_to", link)
    monkeypatch.setattr(tool_install.os, "replace", publish)

    def verify(directory):
        observe("verified")
        assert directory == c.versions[-1] / "tools"
        assert all((c.bin / name).read_text() == "old" for name in tool_install.ENTRY_POINTS)

    version = tool_install.install("merged-source", cwd=tmp_path, revision="abc123", verify=verify)
    assert version == c.versions[0] and "abc123" in version.name
    assert events[-1] == "current"
    assert [event for event in events if event in {"fa", "foragent", "current"}] == (
        ["current"] if managed else ["fa", "foragent", "current"])
    for name in tool_install.ENTRY_POINTS:
        assert (c.bin / name).readlink() == c.root / "current/bin" / name
    assert (c.root / "current").readlink() == version
    assert {p.relative_to(c.old): p.read_bytes() for p in c.old.rglob("*") if p.is_file()} == old_files
    assert not list(c.bin.glob(".*.tmp"))
    with (c.machine / "install.lock").open("a") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


@pytest.mark.parametrize("failure", ["verification", "installer", "entry-point"])
def test_failed_install_removes_only_unpublished_version(installation, tmp_path, failure):
    c = installation
    unrelated = make_version(c.root / "20000101-unrelated")
    old_targets = {name: (c.bin / name).readlink() for name in tool_install.ENTRY_POINTS}
    if failure == "installer":
        c.install_error = RuntimeError("build failed")
    if failure == "entry-point":
        c.missing = "foragent"

    def verify(directory):
        assert directory == c.versions[-1] / "tools"
        raise RuntimeError("verification failed")

    with pytest.raises(RuntimeError, match={"verification": "verification failed",
                                           "installer": "build failed",
                                           "entry-point": "entry point missing"}[failure]):
        tool_install.install("source", cwd=tmp_path, revision="bad",
                             verify=verify if failure == "verification" else None)
    assert len(c.versions) == 1 and not c.versions[0].exists()
    assert list(c.root.iterdir()) == [unrelated]
    assert c.old.is_dir()
    for name in tool_install.ENTRY_POINTS:
        assert (c.bin / name).readlink() == old_targets[name]
        assert (c.bin / name).read_text() == "old"


@pytest.mark.parametrize("entry", ["install", "update"])
def test_successful_switch_enqueues_new_python_refresh_for_every_installed_gate(
    installation, tmp_path, monkeypatch, entry,
):
    c = installation
    monkeypatch.setattr(tool_install.sys, "platform", "darwin")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "0")
    agents = tmp_path / "Library/LaunchAgents"
    agents.mkdir(parents=True)
    roots = [tmp_path / "first repo", tmp_path / "second repo"]
    before = {}
    for suffix, root in [("first", roots[0]), ("legacy", roots[0]), ("second", roots[1])]:
        path = agents / f"dev.foragent.{suffix}.plist"
        path.write_bytes(plistlib.dumps({
            "Label": f"dev.foragent.{suffix}",
            "ProgramArguments": [str(c.old / "bin/fa"), "gate", "tick", "--repo-root", str(root)],
            "EnvironmentVariables": dict(launchd.GATE_ENVIRONMENT),
        }))
        before[path] = path.read_bytes()
    (agents / "com.example.other.plist").write_bytes(plistlib.dumps({"Label": "com.example.other"}))
    monkeypatch.setattr(launchd, "_run_launchctl", lambda args: pytest.fail("enqueue must not restart a live tick"))

    def spawn(argv, **kwargs):
        assert (c.root / "current").readlink() == c.versions[-1]
        assert all((c.bin / name).read_text() == "new" for name in tool_install.ENTRY_POINTS)
        assert_locked(c.machine)
        c.spawned.append((argv, kwargs))

    monkeypatch.setattr(launchd.subprocess, "Popen", spawn)
    if entry == "update":
        monkeypatch.setattr(update_check, "lookup", lambda: "v0.1.10")
        monkeypatch.setattr(update_check, "current_version", lambda: "0.1.9")
        monkeypatch.setattr(update_check, "installed_gates", lambda: {})
        assert update_check.update(tmp_path) == 0
    else:
        tool_install.install("source", cwd=tmp_path, revision="new")
    assert {Path(argv[-1]) for argv, _ in c.spawned} == set(roots)
    assert len(c.spawned) == len(roots), "One refresher handles all labels of a gate"
    for argv, kwargs in c.spawned:
        assert argv[:4] == [str(c.versions[-1] / "tools/foragent/bin/python"),
                            "-m", "foragent.launchd", "refresh"]
        assert kwargs["start_new_session"] is True
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["stdout"] is kwargs["stderr"]
        assert kwargs["stdout"].closed
        assert kwargs["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
    assert {path: path.read_bytes() for path in before} == before


def test_failed_verification_never_enqueues_gate_refresh(installation, tmp_path, monkeypatch):
    monkeypatch.setattr(tool_install.sys, "platform", "darwin")
    monkeypatch.setattr(launchd, "installed_jobs", lambda: pytest.fail("refresh before verified switch"))

    def verify(directory):
        raise RuntimeError("verification failed")

    with pytest.raises(RuntimeError, match="verification failed"):
        tool_install.install("source", cwd=tmp_path, revision="bad", verify=verify)
    assert installation.spawned == []


def test_refresh_spawn_failure_keeps_install_and_enqueues_other_gates(installation, tmp_path, monkeypatch, caplog):
    c = installation
    monkeypatch.setattr(tool_install.sys, "platform", "darwin")
    roots = [tmp_path / "first", tmp_path / "second"]
    monkeypatch.setattr(launchd, "installed_jobs", lambda: [SimpleNamespace(repo_root=root) for root in roots])
    attempts = []

    def spawn(argv, **kwargs):
        attempts.append(Path(argv[-1]))
        if len(attempts) == 1:
            raise OSError("spawn failed")
        c.spawned.append((argv, kwargs))

    monkeypatch.setattr(launchd.subprocess, "Popen", spawn)
    version = tool_install.install("source", cwd=tmp_path, revision="new")
    assert (c.root / "current").readlink() == version
    assert attempts == roots and len(c.spawned) == 1
    assert "could not enqueue launchd refresh" in caplog.text


def test_uv_runner_uses_orch_reinstall_subprocess_and_retry_seams(tmp_path, monkeypatch):
    calls, delays = [], []
    environment = {"UV_TOOL_DIR": str(tmp_path / "tools")}

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, int(len(calls) < 3), stdout="installed",
                                           stderr="Directory not empty" if len(calls) < 3 else "")

    monkeypatch.setattr(orch_reinstall.subprocess, "run", run)
    monkeypatch.setattr(orch_reinstall, "sleep", delays.append)
    assert tool_install.run_uv(["tool", "install", "source"], cwd=tmp_path, env=environment) == "installed"
    assert delays == [2.0, 4.0]
    assert len(calls) == 3
    assert all(argv == ["uv", "tool", "install", "source"] and kwargs["cwd"] == tmp_path
               and kwargs["env"] is environment for argv, kwargs in calls)


@pytest.mark.parametrize("failure", ["fa", "foragent", "current"])
def test_partial_switch_failure_keeps_both_versions_usable(installation, tmp_path, monkeypatch, failure):
    c = installation
    replace = os.replace

    def fail_second(source, target):
        if target.name == failure:
            raise OSError("replace failed")
        replace(source, target)

    monkeypatch.setattr(tool_install.os, "replace", fail_second)
    with pytest.raises(OSError, match="replace failed"):
        tool_install.install("source", cwd=tmp_path, revision="new")
    assert (c.bin / "fa").read_text() == "old"
    assert (c.bin / "foragent").read_text() == "old"
    assert c.versions[0].is_dir() and c.old.is_dir()
    assert not list(c.bin.glob(".*.tmp"))
    assert not list(c.root.glob(".*.tmp"))
    monkeypatch.setattr(tool_install.os, "replace", replace)
    version = tool_install.install("retry", cwd=tmp_path, revision="retry")
    assert (c.root / "current").readlink() == version
    assert all((c.bin / name).resolve().is_relative_to(version) for name in tool_install.ENTRY_POINTS)


def test_subsequent_switch_failure_preserves_both_stable_shims(installation, tmp_path, monkeypatch):
    c = installation
    previous = tool_install.install("first", cwd=tmp_path, revision="first")
    shims = {name: (c.bin / name).lstat().st_ino for name in tool_install.ENTRY_POINTS}

    def fail(source, target):
        assert target == c.root / "current"
        raise OSError("switch failed")

    monkeypatch.setattr(tool_install.os, "replace", fail)
    with pytest.raises(OSError, match="switch failed"):
        tool_install.install("second", cwd=tmp_path, revision="second")
    assert (c.root / "current").readlink() == previous
    assert shims == {name: (c.bin / name).lstat().st_ino for name in tool_install.ENTRY_POINTS}
    assert all((c.bin / name).resolve().is_relative_to(previous) for name in tool_install.ENTRY_POINTS)


@pytest.mark.parametrize("failure", [None, "foragent", "current"])
def test_fresh_install_publishes_neither_entry_point_until_current_switch(
    installation, tmp_path, monkeypatch, failure,
):
    c = installation
    for name in tool_install.ENTRY_POINTS:
        (c.bin / name).unlink()
    replace = os.replace

    def publish(source, target):
        assert not any((c.bin / name).exists() for name in tool_install.ENTRY_POINTS)
        if target.name == failure:
            raise OSError("publish failed")
        replace(source, target)
        assert all((c.bin / name).exists() == (target.name == "current") for name in tool_install.ENTRY_POINTS)

    monkeypatch.setattr(tool_install.os, "replace", publish)
    if failure is None:
        tool_install.install("source", cwd=tmp_path, revision="new")
    else:
        with pytest.raises(OSError, match="publish failed"):
            tool_install.install("source", cwd=tmp_path, revision="new")
        assert not any((c.bin / name).exists() for name in tool_install.ENTRY_POINTS)


@pytest.mark.parametrize("process_path", ["exe", "cmdline"])
def test_gc_keeps_current_previous_running_and_at_most_three_older(
    installation, tmp_path, monkeypatch, process_path,
):
    c = installation
    versions = [make_version(c.root / f"2000010{i}-old") for i in range(8)]
    previous, running = versions[0], versions[1]
    for name in tool_install.ENTRY_POINTS:
        (c.bin / name).unlink()
        (c.bin / name).symlink_to(previous / "bin" / name)
    python = running / "tools/foragent/bin/python"
    # venv python commonly links outside the version. Its argv path must still
    # protect it even though resolving that executable points elsewhere.
    if process_path == "cmdline":
        python.unlink()
        python.symlink_to(tmp_path / "system-python")
    info = {"exe": str(python) if process_path == "exe" else "/usr/bin/python",
            "cmdline": [str(python), "-m", "foragent.kick"] if process_path == "cmdline" else []}
    monkeypatch.setattr(tool_install.psutil, "process_iter", lambda *a, **kw: iter([
        SimpleNamespace(info=info),
    ]))
    version = tool_install.install("source", cwd=tmp_path, revision="new")
    assert {path for path in c.root.iterdir() if not path.is_symlink()} == {version, previous, running, versions[-1]}
    assert c.old.exists(), "The original uv installation is outside GC's ownership"


def test_gc_never_removes_running_versions_even_over_retention_budget(installation, tmp_path, monkeypatch):
    c = installation
    running = [make_version(c.root / f"2000010{i}-active") for i in range(5)]
    monkeypatch.setattr(tool_install.psutil, "process_iter", lambda *a, **kw: iter([
        SimpleNamespace(info={"exe": str(path / "tools/foragent/bin/python"), "cmdline": []})
        for path in running
    ]))
    current = tool_install.install("source", cwd=tmp_path, revision="new")
    assert versions(c.root) == {*running, current}


@pytest.mark.parametrize("process_path", ["exe", "cmdline"])
@pytest.mark.parametrize("alias_at", ["version", "venv", "bin"])
def test_gc_keeps_running_venv_through_directory_alias_and_external_python(
    installation, tmp_path, monkeypatch, process_path, alias_at,
):
    c = installation
    older = [make_version(c.root / f"2000010{i}-old") for i in range(8)]
    running = older[0]
    system_python = tmp_path / "system-python"
    system_python.write_text("external interpreter")
    python = running / "tools/foragent/bin/python"
    python.unlink()
    python.symlink_to(system_python)
    target, suffix = {
        "version": (running, "tools/foragent/bin/python"),
        "venv": (running / "tools/foragent", "bin/python"),
        "bin": (python.parent, "python"),
    }[alias_at]
    alias = tmp_path / "legacy-uv"
    alias.symlink_to(target, target_is_directory=True)
    aliased_python = alias / suffix
    assert aliased_python.resolve() == system_python
    info = {"exe": str(aliased_python) if process_path == "exe" else str(system_python),
            "cmdline": [str(aliased_python), "-m", "foragent.kick"] if process_path == "cmdline" else []}
    monkeypatch.setattr(tool_install.psutil, "process_iter", lambda *a, **kw: iter([SimpleNamespace(info=info)]))
    current = tool_install.install("source", cwd=tmp_path, revision="new")
    assert versions(c.root) == {current, running, *older[-2:]}
    assert alias.is_symlink() and system_python.read_text() == "external interpreter"


@pytest.mark.parametrize("failure", ["table", "access", "delete"])
def test_gc_failure_is_logged_and_does_not_fail_the_install(installation, tmp_path, monkeypatch, caplog, failure):
    c = installation
    older = [make_version(c.root / f"2000010{i}-old") for i in range(5)]
    if failure == "table":
        def unavailable(*args, **kwargs):
            raise OSError("process table unavailable")
        monkeypatch.setattr(tool_install.psutil, "process_iter", unavailable)
    elif failure == "access":
        def denied(attrs, *, ad_value):
            return iter([SimpleNamespace(info={"exe": ad_value, "cmdline": ad_value})])
        monkeypatch.setattr(tool_install.psutil, "process_iter", denied)
    else:
        def blocked(path):
            assert_locked(c.machine)
            raise OSError("directory busy")
        monkeypatch.setattr(tool_install.shutil, "rmtree", blocked)
    version = tool_install.install("source", cwd=tmp_path, revision="new")
    assert all(path.exists() for path in older)
    assert (c.bin / "fa").resolve().is_relative_to(version)
    assert "could not" in caplog.text


def test_update_installs_upstream_tag_through_versioned_helper(installation, tmp_path, monkeypatch):
    c = installation
    monkeypatch.setattr(update_check, "lookup", lambda: "v0.1.10")
    monkeypatch.setattr(update_check, "current_version", lambda: "0.1.9")
    monkeypatch.setattr(update_check, "installed_gates", lambda: {})
    assert update_check.update(tmp_path) == 0
    assert c.calls[-1][0][-1] == update_check.INSTALL_SOURCE + "v0.1.10"
    assert (c.bin / "fa").resolve().is_relative_to(c.versions[0])


def test_failed_update_preserves_live_tool_and_does_not_restart_gates(installation, tmp_path, monkeypatch):
    c = installation
    c.install_error = OSError("build failed")
    monkeypatch.setattr(update_check, "lookup", lambda: "v0.1.10")
    monkeypatch.setattr(update_check, "current_version", lambda: "0.1.9")
    monkeypatch.setattr(update_check, "installed_gates", lambda: {tmp_path: []})
    restarts = []
    monkeypatch.setattr(update_check, "_restart", lambda *args: restarts.append(args))
    assert update_check.update(tmp_path) == 1
    assert not restarts and not c.versions[0].exists()
    assert update_check._read_cache().get("installed") is None
    assert all((c.bin / name).read_text() == "old" for name in tool_install.ENTRY_POINTS)


@pytest.mark.parametrize("via", ["argv", "which", "explicit"])
def test_launchd_preserves_stable_symlink_path_in_generated_plist(installation, tmp_path, monkeypatch, via):
    c = installation
    shim = c.bin / "fa"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(launchd.sys, "argv", [str(shim.relative_to(tmp_path)) if via == "argv" else "cli.py"])
    monkeypatch.setattr(launchd.shutil, "which", lambda name: str(shim))
    assert launchd.fa_executable() == shim
    job = launchd.Launchd(tmp_path, repo="owner/repo", home=tmp_path,
                          executable=shim if via == "explicit" else None)
    assert launchd.gate_command(plistlib.loads(job.plist())["ProgramArguments"])[0] == str(shim)


def test_windows_retains_in_place_install_fallback(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(capacity, "machine_root", lambda: tmp_path)
    monkeypatch.setattr(tool_install.sys, "platform", "win32")

    def uv(args, *, cwd, env=None):
        assert_locked(tmp_path)
        calls.append(args)
        return str(tmp_path / "tools")

    monkeypatch.setattr(tool_install, "run_uv", uv)
    verified = []
    assert tool_install.install("source", cwd=tmp_path, revision="new", verify=verified.append) is None
    assert calls == [["tool", "install", "--force", "--reinstall", "source"], ["tool", "dir"]]
    assert verified == [tmp_path / "tools"]
    assert not (tmp_path / "tool-versions").exists()
