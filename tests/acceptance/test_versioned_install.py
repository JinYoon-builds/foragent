"""Acceptance test for versioned-install. Locked by Foragent; do not edit."""

import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import time
from types import SimpleNamespace

import psutil
import pytest

from foragent import capacity, launchd, orch_reinstall


ENTRY_POINTS = ("fa", "foragent")


def make_version(version, label):
    """Model uv's venv and executable links, with a CLI that actually runs."""
    venv = version / "tools/foragent"
    package = venv / "lib/python3.12/site-packages/foragent"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(f"BUILD = '{label}'\n")
    (venv / "bin").mkdir()
    (venv / "bin/python").write_text("fake process executable")
    (version / "bin").mkdir()
    for name in ENTRY_POINTS:
        executable = venv / "bin" / name
        executable.write_text(f"#!/bin/sh\nprintf '%s\\n' '{label}'\n")
        executable.chmod(0o755)
        (version / "bin" / name).symlink_to(executable)
    return version


@pytest.fixture
def installation(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    machine = home / ".local/share/foragent"
    root = machine / "tool-versions"
    root.mkdir(parents=True)
    bin_dir = home / ".local/bin"
    bin_dir.mkdir(parents=True)
    old = make_version(root / "20000101-previous", "old")
    current = root / "current"
    current.symlink_to(old, target_is_directory=True)
    for name in ENTRY_POINTS:
        (bin_dir / name).symlink_to(current / "bin" / name)
    source = tmp_path / "merged-source"
    (source / "src/foragent").mkdir(parents=True)
    (source / "src/foragent/__init__.py").write_text("BUILD = 'new'\n")
    case = SimpleNamespace(home=home, machine=machine, root=root, bin=bin_dir, old=old,
                           current=current, source=source, installs=[], processes=[], corrupt=False,
                           observe=lambda phase: None)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(capacity, "machine_root", lambda: machine)
    monkeypatch.setattr(psutil, "process_iter", lambda *args, **kwargs: iter(case.processes))

    def forbidden(*args, **kwargs):
        pytest.fail(f"Unexpected process or sleep: {args!r}")

    monkeypatch.setattr(time, "sleep", forbidden)
    monkeypatch.setattr(orch_reinstall, "sleep", forbidden)
    real_run, real_popen = subprocess.run, subprocess.Popen

    def local_popen(argv, *args, **kwargs):
        assert Path(argv[0]) in {bin_dir / name for name in ENTRY_POINTS}, argv
        return real_popen(argv, *args, **kwargs)

    def run(argv, *args, **kwargs):
        if argv[0] != "uv":
            return real_run(argv, *args, **kwargs)
        case.observe("uv-start")
        if argv[1:] == ["tool", "dir", "--bin"]:
            return subprocess.CompletedProcess(argv, 0, stdout=str(bin_dir), stderr="")
        assert argv == ["uv", "tool", "install", "--force", str(source)], argv
        assert Path(kwargs["cwd"]) == source
        environment = kwargs["env"]
        version = Path(environment["UV_TOOL_DIR"]).parent
        assert version.parent == root and version != old
        assert environment["UV_TOOL_BIN_DIR"] == str(version / "bin")
        case.installs.append(make_version(version, "new"))
        installed = version / "tools/foragent/lib/python3.12/site-packages/foragent"
        shutil.copytree(source / "src/foragent", installed, dirs_exist_ok=True)
        if case.corrupt:
            (installed / "__init__.py").write_text("BUILD = 'corrupt'\n")
        case.observe("uv-finished")
        return subprocess.CompletedProcess(argv, 0, stdout="installed", stderr="")

    monkeypatch.setattr(subprocess, "Popen", local_popen)
    monkeypatch.setattr(subprocess, "run", run)
    return case


def cli_versions(case):
    results = []
    for name in ENTRY_POINTS:
        shim = case.bin / name
        assert shim.resolve(strict=True).is_file(), f"Dangling {name} shim"
        assert os.access(shim, os.X_OK), f"Non-executable {name} shim"
        result = subprocess.run([str(shim)], capture_output=True, text=True, check=True)
        results.append(result.stdout.strip())
    return tuple(results)


@pytest.mark.parametrize("layout", ["legacy-shims", "current-shims"], ids=["legacy-shims", "current-shims"])
def test_shims_keep_working_during_install_and_verification_then_switch_current_once(
    installation, monkeypatch, layout,
):
    from foragent import tool_install

    case = installation
    if layout == "legacy-shims":
        case.current.unlink()
        for name in ENTRY_POINTS:
            (case.bin / name).unlink()
            (case.bin / name).symlink_to(case.old / "bin" / name)
    verified = False
    observations, switches = [], []

    def observe(phase):
        labels = cli_versions(case)
        assert labels in (("old", "old"), ("new", "new")), "Both commands must use one working version"
        if not verified:
            assert labels == ("old", "old"), "Keep serving the old CLI until verification succeeds"
        observations.append((phase, labels))

    case.observe = observe
    real_symlink, real_replace = Path.symlink_to, os.replace

    def symlink(path, target, **kwargs):
        observe("before-link")
        real_symlink(path, target, **kwargs)
        observe("after-link")

    def replace(source, target):
        assert verified, "Publish only the verified version"
        assert source.is_symlink() and source.parent == target.parent
        before = cli_versions(case)
        real_replace(source, target)
        after = cli_versions(case)
        if target == case.current:
            switches.append((before, after))
        else:
            assert target in {case.bin / name for name in ENTRY_POINTS}
            assert before == after == ("old", "old"), "Shim migration must still serve the old version"
        observe("after-replace")

    monkeypatch.setattr(Path, "symlink_to", symlink)
    monkeypatch.setattr(os, "replace", replace)

    def verify(directory):
        nonlocal verified
        observe("verify-start")
        assert directory == case.installs[0] / "tools"
        tool_install.verify_package(case.source / "src/foragent", directory)
        observe("verify-finished")
        verified = True

    version = tool_install.install(case.source, cwd=case.source, revision="merged", verify=verify)

    assert version == case.installs[0]
    assert switches == [(("old", "old"), ("new", "new"))], "Switch current in exactly one atomic replace"
    assert {phase for phase, _ in observations} >= {"uv-start", "uv-finished", "verify-start", "verify-finished"}
    assert case.current.readlink() == version
    assert cli_versions(case) == ("new", "new")
    assert case.old.is_dir()
    for name in ENTRY_POINTS:
        assert (case.bin / name).readlink() == case.current / "bin" / name


def test_failed_package_verification_keeps_current_and_old_cli_working(installation):
    from foragent import tool_install

    case = installation
    case.corrupt = True
    previous_target = case.current.readlink()
    previous_shims = {name: (case.bin / name).readlink() for name in ENTRY_POINTS}
    observed = []

    def verify(directory):
        observed.append(directory)
        assert case.current.readlink() == previous_target
        assert cli_versions(case) == ("old", "old")
        tool_install.verify_package(case.source / "src/foragent", directory)

    with pytest.raises(RuntimeError, match="installed package differs from merged source: __init__.py"):
        tool_install.install(case.source, cwd=case.source, revision="bad", verify=verify)

    assert observed == [case.installs[0] / "tools"], "Exercise real package verification after uv succeeds"
    assert case.current.readlink() == previous_target
    assert {name: (case.bin / name).readlink() for name in ENTRY_POINTS} == previous_shims
    assert cli_versions(case) == ("old", "old")
    assert not case.installs[0].exists(), "Discard only the failed unpublished version"


def test_gc_retains_current_previous_and_running_version_with_three_older_limit(installation):
    from foragent import tool_install

    case = installation
    running = make_version(case.root / "19990101-running", "running")
    older = [make_version(case.root / f"2000010{index}-unused", "unused") for index in range(2, 9)]
    case.processes = [SimpleNamespace(info={
        "exe": str(running / "tools/foragent/bin/python"), "cmdline": [],
    })]

    version = tool_install.install(
        case.source, cwd=case.source, revision="gc",
        verify=lambda directory: tool_install.verify_package(case.source / "src/foragent", directory),
    )

    retained = {path for path in case.root.iterdir() if path.is_dir() and not path.is_symlink()}
    assert {version, case.old, running} <= retained, "Protect current, previous and the running executable"
    assert len(retained - {version}) <= 3, "Protected older versions count toward the retention budget"
    assert any(not path.exists() for path in older), "A successful install must actually collect old versions"
    assert (running / "tools/foragent/bin/python").is_file()
    assert case.current.readlink() == version
    assert cli_versions(case) == ("new", "new")


def test_launchd_reports_resolved_version_path_stale_even_with_current_environment(installation, monkeypatch):
    case = installation
    repo = case.source.resolve()
    agents = case.home / "Library/LaunchAgents"
    agents.mkdir(parents=True)
    shim = case.bin / "fa"
    pinned = agents / "dev.foragent.pinned.plist"
    stable = agents / "dev.foragent.stable.plist"
    for path, executable in ((pinned, shim.resolve()), (stable, shim)):
        path.write_bytes(plistlib.dumps({
            "Label": path.stem,
            "ProgramArguments": [str(executable), "gate", "tick", "--repo-root", str(repo)],
            "EnvironmentVariables": dict(launchd.GATE_ENVIRONMENT),
        }))
    before = {path: path.read_bytes() for path in (pinned, stable)}
    assert all(launchd.has_gate_environment(path) for path in (pinned, stable))

    assert {job.plist_path for job in launchd.stale_jobs(repo, home=case.home)} == {pinned}
    assert launchd.needs_environment_refresh(pinned)
    assert not launchd.needs_environment_refresh(stable)
    monkeypatch.setattr(launchd.sys, "argv", [str(shim.resolve())])
    assert launchd.fa_executable() == shim, "Prefer the stable PATH shim over the running version's path"
    assert {path: path.read_bytes() for path in (pinned, stable)} == before
