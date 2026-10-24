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


# Every test fails closed, including signal 0 and subprocess timeout cleanup.
@pytest.fixture(autouse=True)
def no_real_signals(monkeypatch):
    from foragent import processes

    def forbidden(*args, **kwargs):
        pytest.fail(f"Real signals are forbidden: {args!r}")

    for name in ("kill", "killpg"):
        monkeypatch.setattr(os, name, forbidden)
    for name in ("pthread_kill", "raise_signal"):
        monkeypatch.setattr(signal, name, forbidden)
    # pidfd tests install fake descriptors explicitly, on every platform.
    monkeypatch.setattr(os, "pidfd_open", None, raising=False)
    monkeypatch.setattr(signal, "pidfd_send_signal", forbidden, raising=False)
    # Unit tests opt in to the fake process table; never inspect the owner's PIDs.
    monkeypatch.setattr(processes, "list_processes", lambda: ())
    monkeypatch.setattr(processes, "read_process_identity", lambda pid: next(
        (row for row in processes.list_processes() if row.pid == pid), None,
    ))
    from process_fakes import _ModuleView
    from foragent import capacity, cli, scheduler, watch_open

    def probe(pid, signum):
        assert signum == 0, "A signal must use an explicit fake process table"
        if pid != os.getpid():
            raise ProcessLookupError(pid)

    for module in (capacity, cli, scheduler, watch_open):
        monkeypatch.setattr(module, "os", _ModuleView(os, kill=probe))


from process_fakes import fake_process_table  # noqa: E402, F401


@pytest.fixture(autouse=True)
def no_view_windows(monkeypatch):
    """Never open real terminal windows during the test suite."""
    monkeypatch.setenv("FORAGENT_VIEW", "off")


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
