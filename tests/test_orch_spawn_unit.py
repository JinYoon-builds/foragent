"""Boundaries of the gate's orchestrator spawn (D76) beyond the acceptance file."""

from datetime import UTC, datetime, timedelta
import shutil
import subprocess

from foragent import notify, orch_ensure, orch_spawn
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


def test_explicit_call_skips_only_the_spacing(tmp_path, monkeypatch):
    store, repo, calls, _pushes = _setup(tmp_path, monkeypatch, fail=True)
    config = Config(repo="owner/repo", language="en")
    assert orch_spawn.ensure_orchestrator(repo, store, config, now=NOW) is None
    later = NOW + timedelta(seconds=60)
    assert orch_spawn.ensure_orchestrator(repo, store, config, now=later, spacing=False) is None
    assert len(calls) == 2
    monkeypatch.setenv("FORAGENT_ORCH_SPAWN", "off")
    assert orch_spawn.ensure_orchestrator(repo, store, config, now=later, spacing=False) is None
    assert len(calls) == 2


def _explicit_lock_doing(monkeypatch, before, *, busy=False):
    """Run ``before()`` while the explicit call waits for the lock, then report ``busy``."""
    from contextlib import contextmanager

    real = orch_ensure._explicit_lock

    @contextmanager
    def lock(store):
        before()
        with real(store):
            yield busy

    monkeypatch.setattr(orch_ensure, "_explicit_lock", lock)


def _write_spawn(store, at):
    import json

    (store.root / "orch-spawn.json").write_text(json.dumps({"spawned_at": at.isoformat(), "screen": "tmux"}))


def test_explicit_call_never_opens_a_second_after_a_spawn_made_while_it_waited(tmp_path, monkeypatch):
    store, repo, calls, pushes = _setup(tmp_path, monkeypatch)
    _explicit_lock_doing(monkeypatch, lambda: _write_spawn(store, NOW))
    why = []
    assert orch_spawn.ensure_orchestrator(repo, store, Config(repo="owner/repo"), now=NOW,
                                          spacing=False, why=why) is None
    assert calls == pushes == [] and why == ["raced"]


def test_explicit_call_waits_out_an_opener_that_was_mid_spawn_when_it_began(tmp_path, monkeypatch):
    store, repo, calls, _pushes = _setup(tmp_path, monkeypatch)
    _write_spawn(store, NOW - timedelta(seconds=5))  # Written before this call read the record.
    _explicit_lock_doing(monkeypatch, lambda: None, busy=True)
    why = []
    assert orch_spawn.ensure_orchestrator(repo, store, Config(repo="owner/repo"), now=NOW,
                                          spacing=False, why=why) is None
    assert calls == [] and why == ["raced"]


def test_explicit_call_skips_spacing_when_the_busy_lock_held_no_recent_spawn(tmp_path, monkeypatch):
    store, repo, calls, _pushes = _setup(tmp_path, monkeypatch)
    _write_spawn(store, NOW - timedelta(minutes=5))
    _explicit_lock_doing(monkeypatch, lambda: None, busy=True)
    assert orch_spawn.ensure_orchestrator(repo, store, Config(repo="owner/repo"), now=NOW,
                                          spacing=False) == "background"
    assert len(calls) == 1


def test_explicit_call_checks_its_guards_again_inside_the_lock(tmp_path, monkeypatch):
    store, repo, calls, _pushes = _setup(tmp_path, monkeypatch)
    _explicit_lock_doing(monkeypatch, lambda: store.write_heartbeat("orchestrator", NOW))
    why = []
    assert orch_spawn.ensure_orchestrator(repo, store, Config(repo="owner/repo"), now=NOW,
                                          spacing=False, why=why) is None
    assert calls == [] and why == ["alive"]


def test_gate_path_is_unchanged_by_the_explicit_call(tmp_path, monkeypatch):
    """The gate checks liveness before the lock only and keeps its record shape."""
    from contextlib import contextmanager
    import json

    from foragent import watch_open

    store, repo, calls, _pushes = _setup(tmp_path, monkeypatch)
    real = watch_open._opening

    @contextmanager
    def opening(target):
        store.write_heartbeat("orchestrator", NOW)
        with real(target):
            yield

    monkeypatch.setattr(watch_open, "_opening", opening)
    assert orch_spawn.ensure_orchestrator(repo, store, Config(repo="owner/repo"), now=NOW) == "background"
    assert len(calls) == 1
    assert json.loads((store.root / "orch-spawn.json").read_text()) == {
        "spawned_at": NOW.isoformat(), "screen": "background", "launched": True}


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
    # Past orch_silent_alert_minutes (30) the silent session pushes once (#952), never a spawn.
    assert calls == [] and len(pushes) == 1 and "running but silent" in pushes[0]
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


def test_explicit_lock_reports_busy_while_another_opener_holds_it(tmp_path, monkeypatch):
    import threading

    from foragent import watch_open

    store, _repo, _calls, _pushes = _setup(tmp_path, monkeypatch)
    held, release = threading.Event(), threading.Event()

    def other_opener():
        with watch_open._opening(store):
            held.set()
            release.wait(5)

    thread = threading.Thread(target=other_opener)
    thread.start()
    held.wait(5)
    threading.Timer(0.2, release.set).start()
    with orch_ensure._explicit_lock(store) as busy:
        assert busy is True
    thread.join(5)
    with orch_ensure._explicit_lock(store) as busy:
        assert busy is False


def test_spawn_loads_the_orchestrator_role_agent_only_when_the_project_defines_it(tmp_path, monkeypatch):
    store, repo, calls, pushes = _setup(tmp_path, monkeypatch)
    root = repo.resolve()
    assert orch_spawn._command(root, store, "t")[1][-1:] == ["/fa-orchestrator"]
    assert "--agent" not in orch_spawn._command(root, store, "t")[1]
    (root / ".claude/agents").mkdir(parents=True)
    (root / ".claude/agents/fa-orchestrator.md").write_text("---\nname: fa-orchestrator\n---\n")
    argv = orch_spawn._command(root, store, "t")[1]
    assert argv[-3:] == ["--agent", "fa-orchestrator", "/fa-orchestrator"]


def _pushed(store):
    import json

    path = store.root / "orch-silent-alert.json"
    return path.exists() and json.loads(path.read_text()).get("pushed_at") is not None


def _silent_session(tmp_path, monkeypatch):
    """A live heartbeat-less orchestrator, found by the argv scan only (#952)."""
    from foragent import processes

    store, repo, calls, pushes = _setup(tmp_path, monkeypatch)
    store.write_heartbeat("orchestrator", NOW - timedelta(minutes=40))
    rows = [(41, 1.0)]
    monkeypatch.setattr(processes, "orchestrator_process_alive", lambda root, skill="": bool(rows))
    monkeypatch.setattr(processes, "orchestrator_process_starts", lambda root, skill="": list(rows))
    return store, repo, calls, pushes, rows


def test_silence_push_that_is_not_delivered_is_tried_again(tmp_path, monkeypatch):
    store, repo, calls, pushes, _rows = _silent_session(tmp_path, monkeypatch)
    config = Config(repo="owner/repo", language="en")
    for outcome in (False, RuntimeError("down")):
        def send(config, title, line, link=None, outcome=outcome):
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        monkeypatch.setattr(notify, "send", send)
        assert orch_spawn.ensure_orchestrator(repo, store, config, now=NOW) is None
    monkeypatch.setattr(notify, "send", lambda config, title, line, link=None: pushes.append(line) or True)
    assert orch_spawn.ensure_orchestrator(repo, store, config, now=NOW) is None
    assert orch_spawn.ensure_orchestrator(repo, store, config, now=NOW + timedelta(minutes=5)) is None
    assert calls == [] and len(pushes) == 1


def test_spawn_off_turns_the_silence_check_off(tmp_path, monkeypatch):
    store, repo, _calls, pushes, _rows = _silent_session(tmp_path, monkeypatch)
    monkeypatch.setenv("FORAGENT_ORCH_SPAWN", "off")
    config = Config(repo="owner/repo", language="en")
    assert orch_spawn.ensure_orchestrator(repo, store, config, now=NOW) is None
    assert orch_spawn.ensure_orchestrator(repo, store, config, now=NOW, spacing=False) is None
    assert pushes == [] and not (store.root / "orch-silent-note.json").exists()


def test_new_live_orchestrator_process_starts_a_new_episode(tmp_path, monkeypatch):
    store, repo, _calls, pushes, rows = _silent_session(tmp_path, monkeypatch)
    config = Config(repo="owner/repo", language="ko")
    assert orch_spawn.ensure_orchestrator(repo, store, config, now=NOW) is None
    assert len(pushes) == 1 and "오케스트레이터가 조용해요" in pushes[0]
    later = NOW + timedelta(minutes=10)
    rows[:] = [(42, later.timestamp())]  # A different session began; its silence starts at its start.
    assert orch_spawn.ensure_orchestrator(repo, store, config, now=later + timedelta(minutes=29)) is None
    assert len(pushes) == 1
    assert orch_spawn.ensure_orchestrator(repo, store, config, now=later + timedelta(minutes=31)) is None
    assert len(pushes) == 2


def test_explicit_ensure_shares_the_silence_push_and_rearm(tmp_path, monkeypatch):
    store, repo, calls, pushes, _rows = _silent_session(tmp_path, monkeypatch)
    config = Config(repo="owner/repo", language="en")
    why = []
    assert orch_spawn.ensure_orchestrator(repo, store, config, now=NOW, spacing=False, why=why) is None
    assert why == ["session"] and len(pushes) == 1 and "orch: repo" in pushes[0]
    assert orch_spawn.ensure_orchestrator(repo, store, config, now=NOW, spacing=False) is None
    assert len(pushes) == 1
    store.write_heartbeat("orchestrator", NOW + timedelta(minutes=1))
    assert orch_spawn.ensure_orchestrator(repo, store, config, now=NOW + timedelta(minutes=1), spacing=False) is None
    assert not (store.root / "orch-silent-alert.json").exists()
    store.write_heartbeat("orchestrator", NOW - timedelta(hours=1))
    assert orch_spawn.ensure_orchestrator(repo, store, config, now=NOW + timedelta(minutes=2), spacing=False) is None
    assert calls == [] and len(pushes) == 2


def test_no_work_keeps_an_open_episode_open(tmp_path, monkeypatch):
    store, repo, _calls, pushes, _rows = _silent_session(tmp_path, monkeypatch)
    config = Config(repo="owner/repo", language="en")
    assert orch_spawn.ensure_orchestrator(repo, store, config, now=NOW) is None
    for path in (store.root / "approvals").glob("*.json"):
        path.unlink()
    assert orch_spawn.ensure_orchestrator(repo, store, config, now=NOW + timedelta(minutes=5)) is None
    assert _pushed(store) and len(pushes) == 1


def test_concurrent_checks_push_one_episode_once_and_rearm_waits_for_the_record(tmp_path, monkeypatch):
    """Class: concurrent episode updates (gate vs ``fa orch ensure``, check vs rearm)."""
    import threading

    from foragent import orch_silent

    store, repo, _calls, pushes, _rows = _silent_session(tmp_path, monkeypatch)
    config = Config(repo="owner/repo", language="en")
    entered, release = threading.Event(), threading.Event()

    def slow_send(config, title, line, link=None):
        pushes.append(line)
        entered.set()
        release.wait(5)
        return True

    monkeypatch.setattr(notify, "send", slow_send)

    def during_push(other):
        entered.clear()
        release.clear()
        first = threading.Thread(target=orch_silent.check, args=(repo, store, config, NOW))
        first.start()
        assert entered.wait(5)
        other.start()
        other.join(0.3)
        release.set()
        first.join(5)
        other.join(5)

    during_push(threading.Thread(target=orch_silent.check, args=(repo, store, config, NOW)))
    assert len(pushes) == 1, "Two concurrent checks must not both push one episode"
    orch_silent.rearm(store)
    during_push(threading.Thread(target=orch_silent.rearm, args=(store,)))
    assert len(pushes) == 2
    assert not (store.root / "orch-silent-alert.json").exists(), "A rearm during a push must not be overwritten"


def test_lost_observations_do_not_start_a_new_episode(tmp_path, monkeypatch):
    """Class: observation loss (failed scan, shrinking list, unreadable spawn record)."""
    store, repo, calls, pushes, rows = _silent_session(tmp_path, monkeypatch)
    spawn = store.root / "orch-spawn.json"
    _write_spawn(store, NOW - timedelta(hours=2))
    config = Config(repo="owner/repo", language="en")
    rows[:] = [(41, 1.0), (43, 2.0)]
    assert orch_spawn.ensure_orchestrator(repo, store, config, now=NOW) is None
    assert len(pushes) == 1
    for step, change in enumerate((
        lambda: rows.__setitem__(slice(None), [(41, 1.0)]),            # one process unreadable or gone
        lambda: rows.__setitem__(slice(None), [(41, 1.0), (43, 2.0)]),  # readable again
        lambda: spawn.write_text("{broken"),                            # unreadable spawn record
        lambda: _write_spawn(store, NOW - timedelta(hours=2)),          # readable again
    ), start=1):
        change()
        assert orch_spawn.ensure_orchestrator(repo, store, config, now=NOW + timedelta(minutes=step)) is None
    assert calls == [] and len(pushes) == 1


def test_inner_guard_reason_takes_the_shared_silence_step(tmp_path, monkeypatch):
    """Class: skipped shared observation (the inner ``alive`` and ``session`` returns)."""
    from foragent import orch_silent

    store, repo, calls, pushes, _rows = _silent_session(tmp_path, monkeypatch)
    config = Config(repo="owner/repo", language="en")
    real = orch_ensure.skip_reason
    answers = []

    def skip_reason(root, store, now):
        return answers.pop(0) if answers else real(root, store, now)

    monkeypatch.setattr(orch_ensure, "skip_reason", skip_reason)
    answers[:] = [None]  # The outer guard sees nothing; the inner guard sees the silent session.
    why = []
    assert orch_spawn.ensure_orchestrator(repo, store, config, now=NOW, spacing=False, why=why) is None
    assert why == ["session"] and len(pushes) == 1
    assert _pushed(store)
    answers[:] = [None, "alive"]  # A fresh heartbeat first seen by the inner guard rearms.
    assert orch_spawn.ensure_orchestrator(repo, store, config, now=NOW, spacing=False) is None
    assert not (store.root / "orch-silent-alert.json").exists()
    assert orch_silent.check(repo.resolve(), store, config, NOW + timedelta(minutes=1))
    assert calls == [] and len(pushes) == 2


def test_a_changed_session_that_began_before_the_push_starts_a_new_episode(tmp_path, monkeypatch):
    """Class: missed session changes, for both identity sources (argv scan and recorded session)."""
    from foragent import processes

    store, repo, calls, pushes, rows = _silent_session(tmp_path, monkeypatch)
    config = Config(repo="owner/repo", language="en")
    assert orch_spawn.ensure_orchestrator(repo, store, config, now=NOW) is None
    assert len(pushes) == 1
    rows[:] = [(52, 3.0)]  # Another scanned process; it began long before the push.
    assert orch_spawn.ensure_orchestrator(repo, store, config, now=NOW + timedelta(minutes=1)) is None
    assert len(pushes) == 2
    assert orch_spawn.ensure_orchestrator(repo, store, config, now=NOW + timedelta(minutes=2)) is None
    assert len(pushes) == 2
    # A changed recorded session (still before the push) while the scan shows nothing new.
    monkeypatch.setattr(processes, "read_process_identity",
                        lambda pid: processes.ProcessIdentity(pid, 1, pid, pid, 4.0))
    store.write_heartbeat("orchestrator", NOW - timedelta(minutes=40), session={"pid": 61, "start_time": 4.0})
    assert orch_spawn.ensure_orchestrator(repo, store, config, now=NOW + timedelta(minutes=3)) is None
    assert calls == [] and len(pushes) == 3


def test_a_heartbeat_renewed_after_the_liveness_check_rearms(tmp_path, monkeypatch):
    """Class: inconsistent freshness observations (gate and ``fa orch ensure`` through observe_silence)."""
    import json

    store, repo, _calls, pushes, _rows = _silent_session(tmp_path, monkeypatch)
    config = Config(repo="owner/repo", language="en")
    assert orch_spawn.ensure_orchestrator(repo, store, config, now=NOW) is None
    assert _pushed(store) and len(pushes) == 1
    note = (store.root / "orch-silent-note.json").read_text()
    for spacing in (True, False):
        later = NOW + timedelta(hours=2)
        real = orch_spawn._alive
        # The caller saw a stale heartbeat; it is renewed before the silence step reads it.
        calls_seen = []

        def alive(store, now, real=real, calls_seen=calls_seen):
            calls_seen.append(now)
            if len(calls_seen) == 1:  # Only the caller's own check saw the old heartbeat.
                store.write_heartbeat("orchestrator", later)
                return False
            return real(store, now)

        monkeypatch.setattr(orch_spawn, "_alive", alive)
        assert orch_spawn.ensure_orchestrator(repo, store, config, now=later, spacing=spacing) is None
        monkeypatch.setattr(orch_spawn, "_alive", real)
        assert not (store.root / "orch-silent-alert.json").exists(), "A fresh heartbeat must rearm"
        assert (store.root / "orch-silent-note.json").read_text() == note, "Fresh is not silent"
        store.write_heartbeat("orchestrator", NOW - timedelta(minutes=40))
        assert orch_spawn.ensure_orchestrator(repo, store, config, now=NOW, spacing=spacing) is None
        assert _pushed(store)
    assert len(pushes) == 3
    assert json.loads((store.root / "orch-silent-alert.json").read_text())["pushed_at"] == NOW.isoformat()


def test_the_silence_check_rereads_freshness_under_its_lock(tmp_path, monkeypatch):
    """Class: inconsistent freshness observations (renewed after observe_silence's own read)."""
    from foragent import orch_silent

    store, repo, _calls, pushes, _rows = _silent_session(tmp_path, monkeypatch)
    config = Config(repo="owner/repo", language="en")
    assert orch_silent.check(repo, store, config, NOW) and _pushed(store)
    store.write_heartbeat("orchestrator", NOW + timedelta(hours=1))
    assert not orch_silent.check(repo, store, config, NOW + timedelta(hours=1))
    assert not (store.root / "orch-silent-alert.json").exists() and len(pushes) == 1
