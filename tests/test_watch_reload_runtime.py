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
