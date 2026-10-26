"""Boundaries of the gate's orchestrator spawn (D76) beyond the acceptance file."""

from datetime import UTC, datetime, timedelta
import shutil
import subprocess

from foragent import notify, orch_spawn
from foragent.config import Config
from foragent.state import StateStore

NOW = datetime(2026, 10, 26, 12, tzinfo=UTC)


def _setup(tmp_path, monkeypatch, *, claude=True, fail=False):
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    store.save_approval(1, body="b", body_sha="s", acceptance_blobs={}, approved_by="human",
                        approved_at=NOW.isoformat())
    calls, pushes = [], []

    def run(argv, *args, **kwargs):
        calls.append(argv)
        if fail:
            raise subprocess.CalledProcessError(1, argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.delenv("FORAGENT_ORCH_SPAWN", raising=False)
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(shutil, "which", lambda name: f"/offline/bin/{name}" if claude and name == "claude" else None)
    monkeypatch.setattr(notify, "send", lambda config, title, line, link=None: pushes.append(line) or True)
    repo = tmp_path / "repo"
    repo.mkdir()
    return store, repo, calls, pushes


def test_failed_spawn_reserves_spacing_without_a_push(tmp_path, monkeypatch):
    store, repo, calls, pushes = _setup(tmp_path, monkeypatch, fail=True)
    config = Config(repo="owner/repo", language="en")
    assert orch_spawn.ensure_orchestrator(repo, store, config, now=NOW) is None
    assert len(calls) == 1 and pushes == []
    assert orch_spawn.ensure_orchestrator(repo, store, config, now=NOW + timedelta(seconds=60)) is None
    assert len(calls) == 1


def test_missing_claude_opens_nothing_and_keeps_no_record(tmp_path, monkeypatch):
    store, repo, calls, pushes = _setup(tmp_path, monkeypatch, claude=False)
    assert orch_spawn.ensure_orchestrator(repo, store, Config(repo="owner/repo"), now=NOW) is None
    assert calls == pushes == []
    assert not (store.root / "orch-spawn.json").exists()


def test_stale_heartbeat_spawns_and_english_push_names_the_repo(tmp_path, monkeypatch):
    store, repo, _calls, pushes = _setup(tmp_path, monkeypatch)
    store.write_heartbeat("orchestrator", NOW - timedelta(seconds=31))
    assert orch_spawn.ensure_orchestrator(repo, store, Config(repo="owner/repo"), now=NOW) == "background"
    assert pushes == ["Opened a new orchestrator for owner/repo (background)."]


def test_tick_hook_reports_through_the_messages_registry(tmp_path, monkeypatch):
    from types import SimpleNamespace

    report = SimpleNamespace(actions=[])
    ctx = SimpleNamespace(repo_root=tmp_path, store=None, config=Config(repo="owner/repo", language="ko"),
                          now=lambda: NOW)

    def broken(*args, **kwargs):
        raise OSError("disk\nfull")

    monkeypatch.setattr(orch_spawn, "ensure_orchestrator", broken)
    assert orch_spawn.on_tick(ctx, report) is report
    monkeypatch.setattr(orch_spawn, "ensure_orchestrator", lambda *args, **kwargs: "orca")
    orch_spawn.on_tick(ctx, report)
    assert report.actions == ["오케스트레이터를 열지 못했어요: disk full", "오케스트레이터를 열었어요 (orca)"]


def test_tick_hook_never_raises_on_an_incomplete_context():
    from types import SimpleNamespace

    report = SimpleNamespace(actions=[])
    assert orch_spawn.on_tick(SimpleNamespace(), report) is report
    assert len(report.actions) == 1 and report.actions[0].startswith("Could not open an orchestrator:")


class _Proc:
    """A psutil.Process stand-in for the liveness readers."""

    def __init__(self, pid, argv, cwd, *, parent=None, name=None, start=100.0):
        self.pid, self._argv, self._cwd, self._parent = pid, argv, cwd, parent
        self._name, self._start = name or (argv[0].rsplit("/", 1)[-1] if argv else ""), start

    def cmdline(self):
        return list(self._argv)

    def cwd(self):
        if isinstance(self._cwd, Exception):
            raise self._cwd
        return "" if self._cwd is None else str(self._cwd)  # psutil reports a missing cwd as "".

    def name(self):
        return self._name

    def parent(self):
        return self._parent

    def create_time(self):
        return self._start


def test_orchestrator_process_needs_skill_argument_and_readable_repo_cwd(tmp_path, monkeypatch):
    import psutil

    from foragent import processes

    repo = tmp_path / "repo"
    repo.mkdir()
    rows = [_Proc(10, ["claude"], repo), _Proc(11, ["claude", "/fa-orchestrator"], psutil.AccessDenied(11)),
            _Proc(12, ["claude", "/fa-orchestrator"], tmp_path)]
    monkeypatch.setattr(psutil, "process_iter", lambda *a, **k: iter(rows))
    assert not processes.orchestrator_process_alive(repo)
    # A missing ("") or relative cwd must not resolve to the gate's own directory.
    monkeypatch.chdir(repo)
    rows += [_Proc(14, ["claude", "/fa-orchestrator"], None), _Proc(15, ["claude", "/fa-orchestrator"], ".")]
    assert not processes.orchestrator_process_alive(repo)
    rows.append(_Proc(13, ["claude", "/fa-orchestrator"], repo))
    assert processes.orchestrator_process_alive(repo)


def test_claude_session_is_the_nearest_claude_ancestor_wherever_it_runs(tmp_path, monkeypatch):
    import psutil

    from foragent import processes

    repo = tmp_path / "repo"
    repo.mkdir()
    outer = _Proc(5, ["claude", "/fa-orchestrator"], repo, start=1.0)
    nearest = _Proc(6, ["node"], repo, parent=outer, name="claude.exe", start=2.0)
    shell = _Proc(7, ["/bin/sh"], repo, parent=nearest)
    me = _Proc(8, ["fa"], repo, parent=shell)
    monkeypatch.setattr(psutil, "Process", lambda pid=None: me)
    assert processes.claude_session() == {"pid": 6, "start_time": 2.0}
    nearest._cwd = psutil.AccessDenied(6)  # Its working directory plays no part.
    assert processes.claude_session() == {"pid": 6, "start_time": 2.0}
    nearest._name, nearest._argv = "node", ["node"]
    assert processes.claude_session() == {"pid": 5, "start_time": 1.0}
    outer._name, outer._argv = "launchd", ["launchd"]
    assert processes.claude_session() is None


def test_live_recorded_session_suppresses_spawn_and_notes_once_per_hour(tmp_path, monkeypatch):
    import json

    from foragent import processes

    store, repo, calls, pushes = _setup(tmp_path, monkeypatch)
    store.write_heartbeat("orchestrator", NOW - timedelta(minutes=5), session={"pid": 4, "start_time": 9.0})
    monkeypatch.setattr(processes, "orchestrator_process_alive", lambda root, skill="": False)
    monkeypatch.setattr(processes, "read_process_identity",
                        lambda pid: processes.ProcessIdentity(pid, 1, pid, pid, 9.0))
    config = Config(repo="owner/repo")
    for minutes in (0, 59, 61):
        assert orch_spawn.ensure_orchestrator(repo, store, config, now=NOW + timedelta(minutes=minutes)) is None
    assert calls == pushes == []
    events = [json.loads(line) for line in (store.root / "orch-events.jsonl").read_text().splitlines()]
    assert len(events) == 2 and all("orchestrator alive without heartbeat" in e["summary"] for e in events)
    # A reused PID (other start time) is not the recorded session.
    monkeypatch.setattr(processes, "read_process_identity",
                        lambda pid: processes.ProcessIdentity(pid, 1, pid, pid, 10.0))
    assert orch_spawn.ensure_orchestrator(repo, store, config, now=NOW + timedelta(hours=2)) == "background"


def test_claude_session_is_recorded_through_a_foragent_gate_between_watch_and_claude(tmp_path, monkeypatch):
    import psutil

    from foragent import processes

    claude = _Proc(5, ["claude", "--resume", "ID"], tmp_path, start=1.0)
    for gate_argv in (["/x/bin/python", "-m", "foragent.cli", "gate", "check-done"], ["/x/bin/fa", "gate", "run-issue"]):
        gate = _Proc(6, gate_argv, tmp_path, parent=claude)
        watch = _Proc(7, ["/x/bin/fa", "orch", "watch"], tmp_path, parent=gate)
        monkeypatch.setattr(psutil, "Process", lambda pid=None, me=watch: me)
        assert processes.claude_session() == {"pid": 5, "start_time": 1.0}


def test_suite_hides_the_real_claude_session_running_pytest():
    """conftest isolates the real ancestry, so tests never record the Claude running them."""
    from foragent import processes

    assert processes.claude_session() is None
