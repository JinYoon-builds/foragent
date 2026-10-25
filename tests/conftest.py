"""Offline Git history fixtures; no working tree commits or pushes are needed."""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import os
import signal
import shutil
import subprocess

import pytest


ACCEPTANCE = "tests/acceptance/test_feature.py"
BASE_TIME = datetime(2026, 10, 20, 8, tzinfo=UTC)


# Block delivering signals, including subprocess timeout cleanup; allow probes.
@pytest.fixture(autouse=True)
def no_real_signals(monkeypatch):
    from foragent import processes

    def probe_only(send_signal, signal_index):
        def guarded(*args, **kwargs):
            if args[signal_index] != 0:
                pytest.fail(f"Real delivering signals are forbidden: {args!r}")
            if send_signal is None:
                raise NotImplementedError("Signal API is unavailable on this platform")
            return send_signal(*args, **kwargs)

        return guarded

    # These APIs take the signal positionally; raise_signal has no target.
    for module, name, signal_index in (
        (os, "kill", 1), (os, "killpg", 1),
        (signal, "pidfd_send_signal", 1), (signal, "pthread_kill", 1),
        (signal, "raise_signal", 0),
    ):
        monkeypatch.setattr(module, name, probe_only(getattr(module, name, None), signal_index), raising=False)
    # pidfd tests install fake descriptors explicitly, on every platform.
    monkeypatch.setattr(os, "pidfd_open", None, raising=False)
    # Unit tests opt in to the fake process table; never inspect the owner's PIDs.
    monkeypatch.setattr(processes, "list_processes", lambda: ())
    monkeypatch.setattr(processes, "read_process_identity", lambda pid: next(
        (row for row in processes.list_processes() if row.pid == pid), None,
    ))


from process_fakes import fake_process_table  # noqa: E402, F401


@pytest.fixture(autouse=True)
def no_view_windows(monkeypatch):
    """Never open real terminal windows during the test suite."""
    monkeypatch.setenv("FORAGENT_VIEW", "off")


@pytest.fixture(autouse=True)
def no_release_lookup(monkeypatch):
    """Never ask GitHub for the latest release during the test suite (#417)."""
    monkeypatch.setenv("FORAGENT_UPDATE_CHECK", "off")


@pytest.fixture(autouse=True)
def no_real_usage_source(request, monkeypatch):
    """A paused executor's recheck must not start the real `codex app-server` or read the real
    Claude snapshot. Tests that supply their own fake server (`codex_server`) keep the real readers."""
    from foragent import usage_recheck

    if "codex_server" not in request.fixturenames and request.module.__name__ != "test_usage_recheck_unit":
        monkeypatch.setattr(usage_recheck, "read_codex", lambda now: None)
        monkeypatch.setattr(usage_recheck, "read_claude", lambda now, interval, since: None)


@pytest.fixture(autouse=True)
def isolated_test_slots(tmp_path, monkeypatch):
    """Keep machine-wide test slots away from the real data root and live runs."""
    from foragent import capacity

    monkeypatch.setattr(capacity, "machine_root", lambda: tmp_path / "machine-data")
    # A gate or `fa test` parent marks its slot as held; that slot is not in this
    # isolated root, so the mark must not turn the tests' own slots into no-ops.
    monkeypatch.delenv(capacity.TEST_SLOT_ENV, raising=False)


@pytest.fixture(autouse=True)
def kicks(monkeypatch):
    """Record event hooks without starting background ticks in any test."""
    from foragent import cli, orch, order, runner

    calls = []
    for module in (cli, orch, order, runner):
        monkeypatch.setattr(module, "kick_tick", calls.append)
    return calls


@pytest.fixture(autouse=True)
def no_forbidden_git_commands(monkeypatch, no_real_signals):
    """Keep the run's command restrictions true even inside temporary repos."""
    popen = subprocess.Popen

    def guarded(args, *positional, **kwargs):
        if isinstance(args, (list, tuple)) and args and args[0] == "git":
            assert not {"commit", "push", "reset", "checkout", "stash"}.intersection(args), args
        return popen(args, *positional, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", guarded)


def git_command(root: Path, *args: str, input: str | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, input=input, check=True, capture_output=True, text=True,
    ).stdout.strip()


@dataclass
class GitHistory:
    origin: Path
    clone: Path
    tip: str | None = None

    def update(self, files: dict[str, str | None], *, at: datetime = BASE_TIME) -> None:
        """Import a new main commit directly into the local bare origin."""
        stream = [
            "commit refs/heads/main\n",
            f"committer Offline Tests <tests@example.com> {int(at.timestamp())} +0000\n",
            "data 6\nUpdate\n",
        ]
        if self.tip is not None:
            stream.append(f"from {self.tip}\n")
        for path, content in files.items():
            if content is None:
                stream.append(f"D {path}\n")
            else:
                stream.append(f"M 100644 inline {path}\ndata {len(content.encode('utf-8'))}\n{content}\n")
        stream.append("\ndone\n")
        git_command(self.origin, "fast-import", "--quiet", input="".join(stream))
        self.tip = git_command(self.origin, "rev-parse", "refs/heads/main")


def _git_history_template(session_root: Path) -> GitHistory:
    root = session_root / "git-history-template"
    root.mkdir()
    origin = root / "origin.git"
    clone = root / "clone"
    git_command(root, "init", "--bare", "--initial-branch=main", str(origin))
    history = GitHistory(origin, clone)
    history.update({ACCEPTANCE: "def test_feature():\n    assert True\n"})
    git_command(root, "clone", str(origin), str(clone))
    return history


@pytest.fixture
def git_history(tmp_path):
    # Signal-safe fixtures permit Git only inside this test's own directory.
    template = _git_history_template(tmp_path)
    origin = shutil.copytree(template.origin, tmp_path / "origin.git")
    clone = shutil.copytree(template.clone, tmp_path / "clone")
    git_command(clone, "config", "remote.origin.url", str(origin))
    return GitHistory(origin, clone, template.tip)


# Locked acceptance files cannot carry marker edits; apply the same marker here.
LOCAL_ONLY_FILES = {
    "tests/test_launchd.py", "tests/test_watch_open.py", "tests/test_doctor_live_unit.py",
    "tests/acceptance/test_doctor_live.py", "tests/acceptance/test_update_check.py",
    "tests/acceptance/test_view_open_terminals.py", "tests/acceptance/test_view_open_triggers.py",
}


def pytest_itemcollected(item):
    root = Path(__file__).resolve().parent.parent
    if item.path.relative_to(root).as_posix() in LOCAL_ONLY_FILES:
        item.add_marker(pytest.mark.local_only)
