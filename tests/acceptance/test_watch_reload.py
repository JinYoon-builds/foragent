"""Acceptance test for watch-reload. Locked by Foragent; do not edit.

The live view must follow the build the gate installs, even when the view itself was
started from another install (for example the first `uv tool install`). Every install
here is a fake folder under the per-test HOME; os.execv and uv are fakes.
"""

from concurrent.futures import Future
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import psutil
import pytest

import foragent
from test_local_truth import record_gh
from test_orch_review_handoff import bind_cli, make_case
from test_skills_autosync import cli_exit
from test_view_reload import installed_build  # noqa: F401  (fixture: the running view's own build)


RUNNING, INSTALLED = "a1b2c3d", "d4e5f6a"
ENTRY_POINTS = ("fa", "foragent")
PYTHON = f"python{sys.version_info.major}.{sys.version_info.minor}"


def make_tool(venv, sha):
    """One uv tool environment: package build metadata and launchers that report that build."""
    package = venv / "lib" / PYTHON / "site-packages/foragent"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(f"__version__ = {foragent.__version__!r}\n")
    (package / "_build.py").write_text(f"GIT_SHA = {sha!r}\n")
    (venv / "bin").mkdir(parents=True, exist_ok=True)
    for name in ENTRY_POINTS:
        launcher = venv / "bin" / name
        launcher.write_text(f"#!/bin/sh\necho 'foragent {foragent.__version__} ({sha})'\n")
        launcher.chmod(0o755)
    return venv


def relink(link, target, **kwargs):
    temporary = link.with_name(f".{link.name}.tmp")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target, **kwargs)
    os.replace(temporary, link)


class Machine:
    """The person's machine: uv's first install, the gate's tool versions and the PATH shims."""

    def __init__(self, monkeypatch):
        from foragent import capacity

        home = Path.home()
        # The gate's data root on this fake machine, as the versioned-install test models it.
        monkeypatch.setattr(capacity, "machine_root", lambda: home / ".local/share/foragent")
        self.home = home
        self.bin = home / ".local/bin"
        self.uv = home / ".local/share/uv/tools/foragent"
        self.root = home / ".local/share/foragent/tool-versions"
        self.current = self.root / "current"
        self.path = f"{self.bin}{os.pathsep}/usr/bin{os.pathsep}/bin"

    def first_run(self, sha):
        """`uv tool install`: the shims on PATH point into uv's own tool environment."""
        make_tool(self.uv, sha)
        self.bin.mkdir(parents=True, exist_ok=True)
        for name in ENTRY_POINTS:
            relink(self.bin / name, self.uv / "bin" / name)

    def dangling_shims(self):
        """The gate's first publish moves the shims to `current` before `current` exists."""
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ENTRY_POINTS:
            relink(self.bin / name, self.current / "bin" / name)

    def gate_install(self, stamp, sha):
        """What a gate reinstall publishes: a new tool version, then one switch of `current`."""
        version = self.root / f"{stamp}-{sha}"
        make_tool(version / "tools/foragent", sha)
        (version / "bin").mkdir()
        for name in ENTRY_POINTS:
            (version / "bin" / name).symlink_to(version / "tools/foragent/bin" / name)
        self.bin.mkdir(parents=True, exist_ok=True)
        relink(self.current, version, target_is_directory=True)
        for name in ENTRY_POINTS:
            relink(self.bin / name, self.current / "bin" / name)
        return version


def build_of(executable):
    """The build an fa launcher actually runs, as a person would see it."""
    result = subprocess.run([str(executable), "--version"], capture_output=True, text=True,
                            env={"PATH": "/usr/bin:/bin"})
    return result.stdout.strip()


def every_fa_on(path):
    return [Path(directory) / "fa" for directory in path.split(os.pathsep)
            if (Path(directory) / "fa").is_file() and os.access(Path(directory) / "fa", os.X_OK)]


def live_view(git_history, directory, monkeypatch, metadata, machine, *, entry, at=None, uv=None):
    """Drive the real CLI and curses loop offline; `at` maps a frame number to a machine change."""
    import curses

    from foragent import watch
    from foragent.watch_open import LOCK_FD_ENV

    directory.mkdir()
    at = at or {}
    case = make_case(git_history, directory, monkeypatch, skill_libraries=[])
    main = bind_cli(case, monkeypatch)
    gh_calls = record_gh(monkeypatch)
    if uv is not None:
        guarded = subprocess.run

        def run(argv, *args, **kwargs):
            if isinstance(argv, (list, tuple)) and argv and str(argv[0]) == "uv":
                return uv(list(argv), **kwargs)
            return guarded(argv, *args, **kwargs)

        monkeypatch.setattr(subprocess, "run", run)
    argv = [str(entry), "gate", "watch", "--repo-root", str(case.root)]
    state = SimpleNamespace(frames=0, seconds=0.0, terminal_active=False, events=[], exec_calls=[])
    metadata.frame = lambda: state.frames
    # Let monkeypatch restore the lock hand-over variable a re-exec sets.
    monkeypatch.setenv(LOCK_FD_ENV, "")
    monkeypatch.delenv(LOCK_FD_ENV)

    class Reexecuted(BaseException):
        """A successful execv never returns to the old process."""

    class Worker:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def submit(self, callback, *args, **kwargs):
            result = Future()
            try:
                result.set_result(callback(*args, **kwargs))
            except BaseException as exc:
                result.set_exception(exc)
            return result

    class Screen:
        def getmaxyx(self):
            return 50, 120

        def getch(self):
            state.frames += 1
            state.seconds = state.frames * 0.3
            if state.frames in at:
                state.events.append(at[state.frames]())
            return ord("q") if state.frames >= 80 else -1

        def __getattr__(self, name):
            return lambda *args: None

    def restored():
        state.terminal_active = False
        state.events.append("restored")

    def terminal(callback, *args):
        state.terminal_active = True
        try:
            return callback(Screen(), *args)
        finally:
            restored()

    def execv(executable, args):
        state.exec_calls.append((str(executable), [str(arg) for arg in args]))
        state.events.append("execv")
        assert not state.terminal_active, "Restore the terminal before replacing the view process"
        raise Reexecuted

    monkeypatch.setenv("PATH", machine.path)
    monkeypatch.setattr(sys, "argv", argv.copy())
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(curses, "wrapper", terminal)
    monkeypatch.setattr(curses, "has_colors", lambda: False)
    monkeypatch.setattr(curses, "curs_set", lambda value: None)
    monkeypatch.setattr(curses, "endwin", restored)
    monkeypatch.setattr(curses, "mousemask", lambda value: (value, 0))
    monkeypatch.setattr(curses, "mouseinterval", lambda value: None)
    monkeypatch.setattr(watch, "ThreadPoolExecutor", Worker)
    monkeypatch.setattr(watch.time, "monotonic", lambda: state.seconds)
    monkeypatch.setattr(os, "execv", execv)
    try:
        assert cli_exit(main, argv[1:]) == 0
    except Reexecuted:
        pass
    assert gh_calls == []
    return state, argv


def assert_reexecs_installed_build_once(state, argv, after):
    assert len(state.exec_calls) == 1, (
        f"Re-exec exactly once after the gate installs a different build: {state.exec_calls}"
    )
    executable, args = state.exec_calls[0]
    assert INSTALLED in build_of(executable), (
        f"Re-exec the fa the gate installed, not the install the view started from: {executable}"
    )
    assert args[1:] == argv[1:], "Keep the original fa gate watch arguments"
    assert after in state.events, "Exercise the machine change before the reload"
    assert state.events.index(after) < state.events.index("restored") < state.events.index("execv"), (
        "Reload only after the gate install, and unwind curses before execv"
    )
    return executable


def test_view_started_from_another_install_follows_the_gate_reinstall(
    git_history, tmp_path, monkeypatch, installed_build,
):
    machine = Machine(monkeypatch)
    machine.first_run(RUNNING)
    machine.gate_install("20261104T004600Z", RUNNING)
    started_from = machine.uv / "bin/fa"
    assert RUNNING in build_of(started_from)

    def reinstall():
        machine.gate_install("20261104T024700Z", INSTALLED)
        return "reinstalled"

    state, argv = live_view(git_history, tmp_path / "view", monkeypatch, installed_build, machine,
                            entry=started_from, at={30: reinstall})

    # The view's own package never changes; only the gate's install does.
    assert installed_build.sha == RUNNING
    assert_reexecs_installed_build_once(state, argv, "reinstalled")


def test_view_behind_the_gate_install_reloads_and_the_new_process_stays(
    git_history, tmp_path, monkeypatch, installed_build,
):
    from foragent import cli

    machine = Machine(monkeypatch)
    machine.first_run(RUNNING)
    version = machine.gate_install("20261104T024700Z", INSTALLED)
    started_from = machine.uv / "bin/fa"

    first, argv = live_view(git_history, tmp_path / "old-process", monkeypatch, installed_build, machine,
                            entry=started_from, at={1: lambda: "started"})
    executable = assert_reexecs_installed_build_once(first, argv, "started")
    assert first.frames < 80, "A view running an older build than the gate installed must not wait for quit"

    # A successful exec cannot return. Model the new process: it runs the installed build
    # from the installed fa, and must not re-exec that same build again.
    monkeypatch.setattr(installed_build, "sha", INSTALLED)
    monkeypatch.setattr(cli, "GIT_SHA", INSTALLED, raising=False)
    second, _ = live_view(git_history, tmp_path / "new-process", monkeypatch, installed_build, machine,
                          entry=executable)
    assert second.exec_calls == [], "The installed build must not restart itself in a loop"
    assert second.frames == 80, "The up-to-date view keeps drawing until the person quits"
    assert machine.current.resolve() == version.resolve()


def test_view_without_a_gate_install_keeps_running_until_the_first_one_is_published(
    git_history, tmp_path, monkeypatch, installed_build,
):
    machine = Machine(monkeypatch)
    machine.first_run(RUNNING)
    assert not machine.root.exists(), "Start with no gate-managed install at all"

    def publish():
        machine.gate_install("20261104T024700Z", INSTALLED)
        return "published"

    def migrate():
        machine.dangling_shims()
        return "dangling"

    state, argv = live_view(git_history, tmp_path / "view", monkeypatch, installed_build, machine,
                            entry=machine.bin / "fa", at={20: migrate, 45: publish})

    assert "dangling" in state.events, "Refresh through the missing and the half-published install"
    assert_reexecs_installed_build_once(state, argv, "published")


def test_first_run_update_leaves_one_fa_on_path_and_the_view_reexecs_that_build(
    git_history, tmp_path, monkeypatch, installed_build,
):
    from foragent import tool_install

    machine = Machine(monkeypatch)
    machine.first_run(RUNNING)
    source = tmp_path / "merged-source"
    (source / "src/foragent").mkdir(parents=True)
    monkeypatch.setattr(psutil, "process_iter", lambda *args, **kwargs: iter([]))

    def uv(argv, *, env=None, **kwargs):
        if argv[1:] == ["tool", "dir", "--bin"]:
            return subprocess.CompletedProcess(argv, 0, stdout=str(machine.bin), stderr="")
        assert argv[1:4] == ["tool", "install", "--force"], argv
        tools, bin_dir = Path(env["UV_TOOL_DIR"]), Path(env["UV_TOOL_BIN_DIR"])
        make_tool(tools / "foragent", INSTALLED)
        bin_dir.mkdir(parents=True, exist_ok=True)
        for name in ENTRY_POINTS:
            (bin_dir / name).symlink_to(tools / "foragent/bin" / name)
        return subprocess.CompletedProcess(argv, 0, stdout="installed", stderr="")

    def update():
        tool_install.install(source, cwd=source, revision="merged")
        return "updated"

    state, argv = live_view(git_history, tmp_path / "view", monkeypatch, installed_build, machine,
                            entry=machine.bin / "fa", at={30: update}, uv=uv)

    executable = assert_reexecs_installed_build_once(state, argv, "updated")
    on_path = every_fa_on(machine.path)
    assert on_path, "The person can still run fa after the update"
    builds = {str(fa): build_of(fa) for fa in on_path}
    assert all(INSTALLED in build for build in builds.values()), (
        f"No stale fa may stay on PATH after the gate's update: {builds}"
    )
    assert build_of(on_path[0]) == build_of(executable), (
        "The fa a person runs and the fa the view re-execs must be the same build"
    )
