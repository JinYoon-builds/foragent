"""A watch.pid whose PID now belongs to a process started after the file was written is stale.

After a reboot the PID in watch.pid can name an unrelated process (2026-10-29: PID 476 from
10-27 became securityd_system). The lane view writes watch.pid only after it has started, so its
process can never be younger than the file. A younger process at that PID is not the view.
"""

import os

import psutil
import pytest

from foragent import watch_open
from foragent.state import StateStore


@pytest.fixture
def opening(tmp_path, monkeypatch):
    monkeypatch.delenv("FORAGENT_VIEW", raising=False)
    for key in ("ORCA_TERMINAL_HANDLE", "TMUX", "WEZTERM_PANE", "KITTY_WINDOW_ID",
                "KITTY_LISTEN_ON", "TERM_PROGRAM"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(watch_open.shutil, "which",
                        lambda name: "/fake/osascript" if name == "osascript" else None)
    calls = []
    monkeypatch.setattr(watch_open, "_call", lambda argv, root: calls.append((argv, root)))

    def probe(pid, sig):
        assert sig == 0, "Acceptance tests must never deliver process signals"
        if pid != os.getpid():
            raise ProcessLookupError(pid)

    monkeypatch.setattr(os, "kill", probe)
    store = StateStore(tmp_path / "state")
    store.root.mkdir()
    return store, calls


def _pid_file(store, written_at):
    path = store.root / "watch.pid"
    path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    os.utime(path, (written_at, written_at))
    return path


def test_pid_reused_by_a_younger_process_does_not_suppress_opening(opening, tmp_path):
    store, calls = opening
    started = psutil.Process(os.getpid()).create_time()
    path = _pid_file(store, started - 3600)
    watch_open.open_view(tmp_path, store)
    assert len(calls) == 1, "A PID taken by a process younger than watch.pid is not a live view"
    assert path.read_text(encoding="utf-8").strip() == str(os.getpid()), (
        "The opener must not rewrite watch.pid; only the view registers itself"
    )


def test_pid_of_an_older_live_process_still_suppresses_opening(opening, tmp_path):
    store, calls = opening
    started = psutil.Process(os.getpid()).create_time()
    _pid_file(store, started + 1)
    watch_open.open_view(tmp_path, store)
    assert calls == [], "The process that wrote watch.pid is still the live view"


def test_registered_view_still_writes_a_plain_pid(tmp_path):
    store = StateStore(tmp_path / "state")
    store.root.mkdir()
    with watch_open.registered(store):
        assert int((store.root / "watch.pid").read_text(encoding="utf-8")) == os.getpid()
