"""Offline Git history fixtures; no working tree commits or pushes are needed."""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import os
import pwd
import signal
import shutil
import subprocess
import tempfile

import pytest


ACCEPTANCE = "tests/acceptance/test_feature.py"
BASE_TIME = datetime(2026, 10, 20, 8, tzinfo=UTC)


# Captured at import, before any test isolates HOME: the HOME pytest started with.
REAL_HOME = Path.home()
ACCOUNT_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)


_HOME = pytest.StashKey[Path]()
_HOME_PATCH = pytest.StashKey[pytest.MonkeyPatch]()


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    """Give every test its own HOME, XDG and Git config directories (#655).

    A hook, not an autouse fixture: pytest does not order same-scope autouse
    fixtures by declaration, so a fixture could still see the starting HOME.
    This runs before any fixture of this test, of any scope, is set up.
    `isolated_home` still works: it points HOME at its own directory on top.
    """
    home = Path(tempfile.mkdtemp(prefix="fa-test-home-")).resolve()
    patch = pytest.MonkeyPatch()
    item.stash[_HOME], item.stash[_HOME_PATCH] = home, patch
    patch.setenv("HOME", str(home))
    for key, directory in (("XDG_CONFIG_HOME", ".config"), ("XDG_DATA_HOME", ".local/share"),
                           ("XDG_STATE_HOME", ".local/state"), ("XDG_CACHE_HOME", ".cache")):
        patch.setenv(key, str(home / directory))
    patch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    patch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    patch.setenv("GIT_TERMINAL_PROMPT", "0")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item):
    """Restore the starting environment only after every fixture has torn down."""
    yield
    patch = item.stash.get(_HOME_PATCH, None)
    if patch is not None:
        patch.undo()
        shutil.rmtree(item.stash[_HOME], ignore_errors=True)


@pytest.fixture
def temporary_home(request):
    """The HOME this test started with (see pytest_runtest_setup)."""
    return request.node.stash[_HOME]


def _real_homes():
    return {path.resolve() for path in (REAL_HOME, ACCOUNT_HOME)}


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_call(item):
    """Fail closed: no test body may run with the real or starting HOME."""
    home = Path.home().resolve()
    if home in _real_homes():
        raise RuntimeError(f"Test would run with the real HOME {home}; HOME isolation failed")


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
    monkeypatch.setenv("FORAGENT_ORCH_SPAWN", "off")  # Nor real orchestrator sessions (D76).
    # A suite run inside an orchestrator session must not write heartbeats under its id (#413).
    for name in ("FORAGENT_SESSION_ID", "FORAGENT_ROLE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def no_real_ancestors(monkeypatch):
    """Hide the real processes above pytest (#590).

    `fa orch watch` records the nearest Claude ancestor. Under a gate check or a
    person's Claude, that ancestor is the real session running pytest; tests
    must not see it. Tests that need ancestors replace psutil.Process themselves.
    """
    import psutil

    own = os.getpid()

    class Isolated(psutil.Process):
        def parent(self):
            return None if self.pid == own else super().parent()

        def parents(self):
            return [] if self.pid == own else super().parents()

    monkeypatch.setattr(psutil, "Process", Isolated)


@pytest.fixture(autouse=True)
def no_real_installed_gates(monkeypatch, temporary_home):
    """Status lists every gate on the machine (#366); never this machine's real gates.

    Under the default temporary HOME (which stands in for the starting HOME) the
    list is empty; a test that sets its own HOME discovers its own gates.
    """
    from foragent import gates

    collect = gates.collect

    def isolated(*args, **kwargs):
        if Path.home() in (REAL_HOME, temporary_home) and kwargs.get("home") is None:
            return []
        return collect(*args, **kwargs)

    monkeypatch.setattr(gates, "collect", isolated)


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
def no_live_cpu_sensors(monkeypatch):
    """Tests opt into fake CPU counters and load averages, independent of host activity."""
    import psutil

    popen = subprocess.Popen

    def isolated_cpu(*args, **kwargs):
        # Child interpreters cannot inherit the parent's monkeypatched counters.
        env = kwargs.get("env")
        kwargs["env"] = {**(os.environ if env is None else env), "FORAGENT_CPU_BUSY": "off"}
        return popen(*args, **kwargs)

    def unavailable(*args, **kwargs):
        raise OSError("CPU counters are unavailable unless the test supplies them")

    monkeypatch.setattr(subprocess, "Popen", isolated_cpu)
    monkeypatch.setattr(psutil, "cpu_times", unavailable)
    # Admission falls back to the load average when CPU counters are unavailable.
    monkeypatch.setattr(os, "getloadavg", lambda: (0.0, 0.0, 0.0))


@pytest.fixture(autouse=True)
def isolated_test_slots(tmp_path, monkeypatch):
    """Keep machine-wide test slots away from the real data root and live runs."""
    from foragent import capacity

    monkeypatch.setattr(capacity, "machine_root", lambda: tmp_path / "machine-data")
    # A gate or `fa test` parent marks its slot as held; that slot is not in this
    # isolated root, so the mark must not turn the tests' own slots into no-ops.
    monkeypatch.delenv(capacity.TEST_SLOT_ENV, raising=False)


@pytest.fixture(autouse=True)
def no_real_machine_sleep(request, monkeypatch):
    """The sensor reports no sleep, so the owner's real sleep never credits runs on fake clocks (#415).

    Tests that supply their own sleep facts read the real sensor or patch it themselves.
    """
    from foragent import capacity

    if request.module.__name__ not in {"test_machine_sleep", "test_sleep_credit"}:
        monkeypatch.setattr(capacity, "slept_seconds", lambda since: 0.0)


@pytest.fixture(autouse=True)
def isolated_github_call_counter():
    """A context's gh counter must not leak into later tests' gh calls."""
    from foragent import gh

    gh.clear_recorder()
    yield
    gh.clear_recorder()


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

    def update(self, files: dict[str, str | None], *, at: datetime = BASE_TIME, message: str = "Update") -> None:
        """Import a new main commit directly into the local bare origin."""
        stream = [
            "commit refs/heads/main\n",
            f"committer Offline Tests <tests@example.com> {int(at.timestamp())} +0000\n",
            f"data {len(message.encode('utf-8'))}\n{message}\n",
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
