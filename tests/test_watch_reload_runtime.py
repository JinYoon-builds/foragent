"""Installed metadata must survive Python caches and replacement gaps."""

import importlib.util
import os
import sys
from types import ModuleType

import pytest

from foragent import watch
from foragent.config import Config
from foragent.state import StateStore
from foragent.tracker import FakeTracker


def test_metadata_reads_replaced_source_even_with_cached_module_and_bytecode(tmp_path, monkeypatch):
    source = tmp_path / "_build.py"
    source.write_text("GIT_SHA = 'aaaaaaa'\n")
    spec = importlib.util.spec_from_file_location("foragent._build", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setitem(sys.modules, "foragent._build", module)
    timestamp = source.stat().st_mtime_ns

    assert watch._installed_build() == "aaaaaaa"
    source.write_text("GIT_SHA = 'bbbbbbb'\n")
    os.utime(source, ns=(timestamp, timestamp))

    assert module.GIT_SHA == "aaaaaaa"
    assert watch._installed_build() == "bbbbbbb"
    source.unlink()
    assert watch._installed_build() == "unknown"


@pytest.mark.parametrize("content", ["GIT_SHA =", "", "GIT_SHA = None", "GIT_SHA = ''"])
def test_partial_install_does_not_request_reload(tmp_path, monkeypatch, content):
    source = tmp_path / "_build.py"
    source.write_text(content)
    module = ModuleType("foragent._build")
    module.__file__ = str(source)
    monkeypatch.setitem(sys.modules, "foragent._build", module)

    assert not watch.should_reload("aaaaaaa", watch._installed_build())


def test_module_launch_reexec_preserves_arguments_after_pid_cleanup(tmp_path, monkeypatch):
    import curses

    store = StateStore(tmp_path / "state")
    argv = [str(watch.Path(watch.__file__).with_name("cli.py")),
            "gate", "watch", "--repo-root", str(tmp_path)]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(watch, "collect", lambda *args: object())
    monkeypatch.setattr(curses, "wrapper", lambda *args: True)
    calls = []

    def execv(executable, args):
        assert not (store.root / "watch.pid").exists()
        calls.append((executable, args))

    monkeypatch.setattr(os, "execv", execv)
    watch.run(lambda: {}, FakeTracker(), store, Config(), tmp_path)

    assert calls == [(sys.executable, [sys.executable, "-m", "foragent.cli", *argv[1:]])]


def test_once_does_not_probe_or_replace_build(tmp_path, monkeypatch, capsys):
    def unexpected():
        pytest.fail("one-shot output must not monitor installation changes")

    monkeypatch.setattr(watch, "_installed_build", unexpected)
    monkeypatch.setattr(watch, "collect", lambda *args: object())
    monkeypatch.setattr(watch, "render_frame", lambda *args: "one frame")
    watch.run(lambda: {}, FakeTracker(), StateStore(tmp_path), Config(), tmp_path, once=True)
    assert capsys.readouterr().out == "one frame\n"


def test_changed_build_restarts_without_waiting_for_a_stuck_refresh(tmp_path, monkeypatch):
    """The real executor must not join a refresh that never finishes before re-exec."""
    import curses
    import threading
    from types import SimpleNamespace

    from foragent import watch_terminal

    release = threading.Event()
    started = threading.Event()
    clock = SimpleNamespace(seconds=0.0)
    builds = iter(["aaaaaaa"])

    def stuck_refresh():
        started.set()
        release.wait(30)

    class Screen:
        def getch(self):
            clock.seconds += 1.0
            return -1

        def getmaxyx(self):
            return 10, 80

        def __getattr__(self, name):
            return lambda *args: None

    monkeypatch.setattr(curses, "has_colors", lambda: False)
    monkeypatch.setattr(curses, "curs_set", lambda value: None)
    monkeypatch.setattr(watch.time, "monotonic", lambda: clock.seconds)
    monkeypatch.setattr(watch, "_installed_build", lambda: next(builds, "bbbbbbb"))
    monkeypatch.setattr(watch, "_frame", lambda *args, **kwargs: [watch._line("lane"), watch._line("help")])
    result = []
    view = threading.Thread(target=lambda: result.append(watch_terminal._terminal(
        Screen(), stuck_refresh, SimpleNamespace(status={"lanes": {}}), Config(), tmp_path, lambda: None)), daemon=True)
    try:
        view.start()
        view.join(5)
        assert started.is_set(), "Exercise a refresh that is still running"
        assert not view.is_alive(), "A stuck refresh blocked the restart"
        assert result == [True]
    finally:
        release.set()


def _gate_version(root, name, sha, *, build=True, launcher=True):
    """One gate tool version under `root`, optionally half-published."""
    version = root / name
    package = version / "tools/foragent/lib/python3.12/site-packages/foragent"
    package.mkdir(parents=True)
    if build:
        (package / "_build.py").write_text(f"GIT_SHA = {sha!r}\n")
    (version / "bin").mkdir()
    if launcher:
        fa = version / "bin" / "fa"
        fa.write_text("#!/bin/sh\n")
        fa.chmod(0o755)
    return version


@pytest.fixture
def gate_root(tmp_path, monkeypatch):
    from foragent import capacity

    monkeypatch.setattr(capacity, "machine_root", lambda: tmp_path / "data")
    root = tmp_path / "data/tool-versions"
    root.mkdir(parents=True)
    return root


def _publish(root, version):
    current = root / "current"
    current.unlink(missing_ok=True)
    current.symlink_to(version, target_is_directory=True)


def test_gate_build_different_from_the_running_one_reexecs_its_launcher(gate_root, monkeypatch):
    from foragent import tool_install, watch_terminal

    version = _gate_version(gate_root, "v2", "bbbbbbb")
    _publish(gate_root, version)
    monkeypatch.setattr(watch, "_installed_build", lambda: "aaaaaaa")

    assert tool_install.installed_tool() == ("bbbbbbb", version.resolve() / "bin" / "fa")
    assert watch_terminal._stale("aaaaaaa") == version.resolve() / "bin" / "fa"
    monkeypatch.setattr(watch, "_installed_build", lambda: "bbbbbbb")
    assert watch_terminal._stale("bbbbbbb") is False, "The installed build must not restart itself"


@pytest.mark.parametrize("state", ["no-root", "no-current", "dangling", "no-build", "no-launcher", "unknown"])
def test_missing_or_half_published_gate_install_reads_as_unknown(gate_root, monkeypatch, state):
    from foragent import tool_install, watch_terminal

    if state == "no-root":
        gate_root.rmdir()
    elif state == "dangling":
        (gate_root / "current").symlink_to(gate_root / "gone", target_is_directory=True)
    elif state != "no-current":
        version = _gate_version(gate_root, "v2", "bbbbbbb" if state != "unknown" else "unknown",
                                build=state != "no-build", launcher=state != "no-launcher")
        _publish(gate_root, version)
    monkeypatch.setattr(watch, "_installed_build", lambda: "aaaaaaa")

    assert tool_install.installed_tool() is None
    assert watch_terminal._stale("aaaaaaa") is False


def test_own_package_reload_stays_the_fallback_without_a_gate_build(gate_root, monkeypatch):
    from foragent import watch_terminal

    monkeypatch.setattr(watch, "_installed_build", lambda: "ccccccc")
    assert watch_terminal._stale("aaaaaaa") is True
    # A gate build equal to the running one still lets the own package reload.
    _publish(gate_root, _gate_version(gate_root, "v1", "aaaaaaa"))
    assert watch_terminal._stale("aaaaaaa") is True


def test_build_and_launcher_come_from_one_version_when_current_switches(gate_root, monkeypatch):
    from foragent import tool_install

    first = _gate_version(gate_root, "v2", "bbbbbbb")
    second = _gate_version(gate_root, "v3", "ccccccc")
    _publish(gate_root, first)
    reads = []
    real = tool_install.runpy.run_path

    def run_path(path):
        reads.append(path)
        _publish(gate_root, second)  # The gate switches while the build is read.
        return real(path)

    monkeypatch.setattr(tool_install.runpy, "run_path", run_path)
    assert tool_install.installed_tool() == ("bbbbbbb", first.resolve() / "bin" / "fa")
    assert reads and reads[0].startswith(str(first.resolve()))


def test_gate_launcher_reexec_keeps_arguments_after_pid_cleanup(tmp_path, monkeypatch):
    import curses

    store = StateStore(tmp_path / "state")
    launcher = tmp_path / "gate/bin/fa"
    argv = [str(tmp_path / "uv/bin/fa"), "gate", "watch", "--repo-root", str(tmp_path)]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(watch, "collect", lambda *args: object())
    monkeypatch.setattr(curses, "wrapper", lambda *args: launcher)
    calls = []

    def execv(executable, args):
        assert not (store.root / "watch.pid").exists()
        calls.append((executable, args))

    monkeypatch.setattr(os, "execv", execv)
    watch.run(lambda: {}, FakeTracker(), store, Config(), tmp_path)

    assert calls == [(str(launcher), [str(launcher), *argv[1:]])]
