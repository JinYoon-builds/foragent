"""Acceptance test for view-reload. Locked by Foragent; do not edit."""

from bisect import bisect_right
from collections import Counter
from concurrent.futures import Future
import importlib
from importlib.abc import Loader, MetaPathFinder
from importlib.util import spec_from_loader
import sys
from types import ModuleType, SimpleNamespace

import pytest

from test_local_truth import record_gh
from test_orch_review_handoff import bind_cli, make_case
from test_skills_autosync import cli_exit, isolated_home


@pytest.fixture
def installed_build(monkeypatch):
    """The --version metadata fake, also importable again after a reinstall."""
    import foragent
    from foragent import cli, watch

    metadata = SimpleNamespace(sha="a1b2c3d", reads=[], frame=lambda: 0)
    build = ModuleType("foragent._build")

    def read(name):
        if name.startswith("__"):
            raise AttributeError(name)
        metadata.reads.append(metadata.frame())
        return metadata.sha

    build.__getattr__ = read

    class BuildImporter(MetaPathFinder, Loader):
        def find_spec(self, fullname, path=None, target=None):
            if fullname == build.__name__:
                return spec_from_loader(fullname, self)
            return None

        def create_module(self, spec):
            return build

        def exec_module(self, module):
            pass

    # Support both a fresh import and importlib.reload without a real installation.
    with monkeypatch.context() as patch:
        patch.setitem(sys.modules, build.__name__, build)
        patch.setattr(foragent, "_build", build, raising=False)
        patch.setattr(sys, "meta_path", [BuildImporter(), *sys.meta_path])
        importlib.reload(watch)
        importlib.reload(cli)
        yield metadata
    importlib.reload(watch)
    importlib.reload(cli)


def live_view(git_history, tmp_path, monkeypatch, metadata, *, reinstall=False):
    """Drive the real CLI and curses callback through several offline refreshes."""
    import curses
    import os

    from foragent import watch

    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[])
    main = bind_cli(case, monkeypatch)
    gh_calls = record_gh(monkeypatch)
    argv = [str(tmp_path / "bin/fa"), "gate", "watch", "--repo-root", str(case.root)]
    state = SimpleNamespace(frames=0, seconds=0.0, refresh_frames=[0],
                            terminal_active=False, events=[], exec_calls=[])
    metadata.frame = lambda: state.frames
    metadata.reads.clear()

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
            state.refresh_frames.append(state.frames)
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
            if reinstall and state.frames == 30:
                metadata.sha = "d4e5f6a"
                state.events.append("reinstalled")
            # A bound keeps missing reload support from hanging the acceptance run.
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
        state.exec_calls.append((executable, list(args)))
        state.events.append("execv")
        assert not state.terminal_active, "Restore the terminal before replacing the view process"
        raise Reexecuted

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


def assert_refresh_polling(state, metadata):
    """Count metadata reads by actual refresh cycle, rather than animation frame."""
    assert len(state.refresh_frames) >= 3, "Exercise startup and at least two later refreshes"
    counts = Counter(bisect_right(state.refresh_frames, frame) - 1 for frame in metadata.reads)
    assert set(range(1, len(state.refresh_frames))) <= counts.keys(), (
        "The open view must read installed build metadata again on later refreshes"
    )
    assert all(count <= 1 for count in counts.values()), (
        f"Read the installed build at most once per refresh cycle, not every animation frame: {counts}"
    )


def test_view_reload_only_a_different_known_build_requires_restart():
    from foragent.watch import should_reload

    assert should_reload("a1b2c3d", "a1b2c3d") is False
    assert should_reload("a1b2c3d", "d4e5f6a") is True
    # A reinstall can also restore an older build; SHA ordering is irrelevant.
    assert should_reload("d4e5f6a", "a1b2c3d") is True


def test_view_reload_missing_metadata_never_requires_restart(installed_build, monkeypatch, capsys):
    import foragent
    from foragent import cli

    with monkeypatch.context() as missing:
        missing.setitem(sys.modules, "foragent._build", None)
        missing.delattr(foragent, "_build", raising=False)
        importlib.reload(cli)
        assert cli_exit(cli.main, ["--version"]) == 0
        assert capsys.readouterr().out.strip() == f"foragent {foragent.__version__} (unknown)"

    from foragent.watch import should_reload

    for started, current in (("a1b2c3d", "unknown"), ("unknown", "d4e5f6a"),
                             ("unknown", "unknown"), ("a1b2c3d", ""), ("", "d4e5f6a"), ("", "")):
        assert should_reload(started, current) is False


def test_view_reload_reinstall_restores_terminal_and_executes_original_argv_once(
    git_history, tmp_path, monkeypatch, installed_build,
):
    state, argv = live_view(git_history, tmp_path, monkeypatch, installed_build, reinstall=True)

    assert state.exec_calls == [(argv[0], argv)], (
        "A reinstalled build must restart the open view once with the original fa gate watch arguments"
    )
    assert state.events.index("reinstalled") < state.events.index("restored") < state.events.index("execv")
    assert_refresh_polling(state, installed_build)


def test_view_reload_unchanged_build_is_checked_once_per_refresh_without_reexec(
    git_history, tmp_path, monkeypatch, installed_build,
):
    state, _ = live_view(git_history, tmp_path, monkeypatch, installed_build)

    assert state.exec_calls == [], "An unchanged installed build must keep the current view running"
    assert_refresh_polling(state, installed_build)
