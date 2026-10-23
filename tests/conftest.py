"""Offline Git history fixtures; no working tree commits or pushes are needed."""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import subprocess

import pytest


ACCEPTANCE = "tests/acceptance/test_feature.py"
BASE_TIME = datetime(2026, 10, 20, 8, tzinfo=UTC)


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
def no_forbidden_git_commands(monkeypatch):
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


@pytest.fixture
def git_history(tmp_path):
    origin = tmp_path / "origin.git"
    clone = tmp_path / "clone"
    git_command(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    history = GitHistory(origin, clone)
    history.update({ACCEPTANCE: "def test_feature():\n    assert True\n"})
    git_command(tmp_path, "clone", str(origin), str(clone))
    return history
