"""Acceptance test for orch-spawn-liveness. Locked by Foragent; do not edit."""

from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess

import psutil
import pytest

from foragent import orch, orch_spawn, processes
from foragent.orch_spawn import ensure_orchestrator
from foragent.state import StateStore
from test_heartbeats import heartbeats_offline, heartbeats_write  # noqa: F401
from test_orch_spawn import orch_spawn_case
from test_orch_review_handoff import bind_cli
from test_stop_whole_tree import events
from test_stop_whole_tree import (
    fake_processes as fake_processes,  # noqa: PLC0414 -- pytest autouse fixture re-export
)


@pytest.fixture
def liveness_case(git_history, tmp_path, monkeypatch, fake_processes):
    case, commands, pushes = orch_spawn_case(
        git_history, tmp_path, monkeypatch, seed_snapshot=False,
    )
    heartbeats_write(case.store, "orch", case.clock[0] - timedelta(minutes=5))

    # Single-PID reads, like listings, must use the fake table only.
    def read_identity(pid):
        try:
            return fake_processes.process(pid)
        except ProcessLookupError:
            return None

    monkeypatch.setattr(processes, "read_process_identity", read_identity)
    # Ancestor readers may use psutil directly; every such read must still
    # come from the same fake table, including psutil's executable name.
    case.process_names = {}

    class TableProcess:
        def __init__(self, pid=None):
            self.pid = fake_processes.self_pid if pid is None else pid
            try:
                self.row = fake_processes.process(self.pid)
            except ProcessLookupError:
                raise psutil.NoSuchProcess(self.pid) from None

        def ppid(self):
            return self.row.ppid

        def cmdline(self):
            return list(self.row.argv)

        def create_time(self):
            return self.row.start_time

        def cwd(self):
            return str(self.row.cwd)

        def name(self):
            return case.process_names.get(self.pid, Path(self.row.argv[0]).name if self.row.argv else "host")

        def status(self):
            return psutil.STATUS_RUNNING

        def parent(self):
            return TableProcess(self.row.ppid) if self.row.ppid in fake_processes.live else None

        def parents(self):
            parents, parent = [], self.parent()
            while parent is not None:
                parents.append(parent)
                parent = parent.parent()
            return parents

        @property
        def info(self):
            return {"pid": self.pid, "ppid": self.ppid(), "cmdline": self.cmdline(),
                    "create_time": self.create_time(), "cwd": self.cwd(), "name": self.name()}

    monkeypatch.setattr(psutil, "Process", TableProcess)
    monkeypatch.setattr(psutil, "process_iter", lambda *args, **kwargs: (
        TableProcess(pid) for pid in sorted(fake_processes.live)
    ))
    monkeypatch.setattr(os, "getpid", lambda: fake_processes.self_pid)
    monkeypatch.setattr(os, "getppid", lambda: fake_processes.parent_pid)
    return case, commands, pushes


def record_heartbeat_session(case, *, pid, start_time):
    path = case.store.root / "orch-heartbeat.json"
    heartbeat = json.loads(path.read_text(encoding="utf-8"))
    heartbeat["session"] = {"pid": pid, "start_time": start_time}
    path.write_text(json.dumps(heartbeat), encoding="utf-8")
    return path


def assert_alive_and_throttled(case, commands, pushes):
    heartbeat = case.store.root / "orch-heartbeat.json"
    before = heartbeat.read_bytes()
    assert ensure_orchestrator(case.root, case.store, case.config, now=case.clock[0]) is None, (
        "A live orchestrator session must prevent a second session despite a stale watch heartbeat"
    )
    assert commands == pushes == [], "Liveness suppression must not call a launcher or send a spawn push"
    first_events = events(case)
    assert len(first_events) == 1, "Record one diagnostic for a live orchestrator without a fresh heartbeat"
    assert "orchestrator alive without heartbeat" in json.dumps(first_events[0]), first_events

    # Exceed spawn spacing, retain the stale session identity, and reopen the
    # store so only durable liveness and diagnostic throttling can suppress it.
    case.clock[0] += timedelta(minutes=10)
    reopened = StateStore(case.store.root, now=lambda: case.clock[0])
    assert ensure_orchestrator(case.root, reopened, case.config, now=case.clock[0]) is None
    assert commands == pushes == []
    assert events(case) == first_events, "The diagnostic must be emitted at most once per hour"
    assert heartbeat.read_bytes() == before, "A liveness check must retain the watch's recorded session"


def launcher_returns_pid(case, table, monkeypatch):
    """Expose a launcher PID and its kernel start time through fake I/O only."""
    run, popen = subprocess.run, subprocess.Popen
    pid, start_time = 88001, case.clock[0].timestamp()

    def identify(result, argv):
        if Path(argv[0]).name != "git":
            table.add(pid, cwd=case.root, start_time=start_time,
                      argv=("/offline/bin/claude", "/fa-orchestrator"))
            result.pid = pid
        return result

    def run_with_pid(argv, *args, **kwargs):
        return identify(run(argv, *args, **kwargs), argv)

    def popen_with_pid(argv, *args, **kwargs):
        return identify(popen(argv, *args, **kwargs), argv)

    monkeypatch.setattr(subprocess, "run", run_with_pid)
    monkeypatch.setattr(subprocess, "Popen", popen_with_pid)
    for name, fake in (("run", run_with_pid), ("Popen", popen_with_pid)):
        if hasattr(orch_spawn, name):
            monkeypatch.setattr(orch_spawn, name, fake)
    return pid, start_time


def test_orch_spawn_liveness_repo_claude_suppresses_spawn_and_throttles_event(
    liveness_case, fake_processes,
):
    case, commands, pushes = liveness_case
    fake_processes.add(
        43000, cwd=case.root.resolve(), start_time=case.clock[0].timestamp() - 3600,
        argv=("/offline/bin/claude", "/fa-orchestrator"),
    )
    # No heartbeat session and no spawn record: the explicit skill argument
    # identifies a gate-spawned orchestrator, unlike a plain planning Claude.
    assert_alive_and_throttled(case, commands, pushes)
    assert not fake_processes.signals


def test_orch_spawn_liveness_recorded_session_survives_a_stale_watch_heartbeat(
    liveness_case, fake_processes, tmp_path,
):
    case, commands, pushes = liveness_case
    session = fake_processes.add(
        43000, cwd=tmp_path / "session-host", start_time=case.clock[0].timestamp() - 3600,
        argv=("/offline/bin/claude",),
    )
    record_heartbeat_session(case, pid=session.pid, start_time=session.start_time)

    # No watch process, /fa-orchestrator argument or orch-spawn.json exists.
    # The stale heartbeat's recorded PID plus start time is the sole evidence.
    assert_alive_and_throttled(case, commands, pushes)
    assert session.pid in fake_processes.live and not fake_processes.signals


def test_orch_spawn_liveness_reused_pid_spawns_and_records_the_new_identity(
    liveness_case, fake_processes, monkeypatch,
):
    case, commands, pushes = liveness_case
    previous_start = case.clock[0].timestamp() - 3600
    heartbeat = record_heartbeat_session(case, pid=43000, start_time=previous_start)
    before = heartbeat.read_bytes()
    record = case.store.root / "orch-spawn.json"
    replacement = fake_processes.add(
        43000, cwd=case.root, start_time=previous_start + 60, argv=("/offline/bin/claude",),
    )
    spawned_pid, spawned_start = launcher_returns_pid(case, fake_processes, monkeypatch)

    assert ensure_orchestrator(case.root, case.store, case.config, now=case.clock[0]) == "background"
    assert len(commands) == len(pushes) == 1, "A reused PID and a plain planning Claude must allow a spawn"
    stored = json.loads(record.read_text(encoding="utf-8"))
    assert stored.get("pid") == spawned_pid, "orch-spawn.json must retain the PID returned by the launcher"
    assert stored.get("start_time") == spawned_start, (
        "orch-spawn.json must retain the spawned session's start time to detect later PID reuse"
    )
    assert stored["spawned_at"] == case.clock[0].isoformat()
    # #413 (2026-10-30): a gate launch writes its new session descriptor into
    # the heartbeat at once, so the bytes change and name the launched session.
    launched = [argv for argv in commands if "--session-id" in argv]
    assert len(launched) == 1, commands
    session_id = launched[0][launched[0].index("--session-id") + 1]
    assert heartbeat.read_bytes() != before
    assert session_id in heartbeat.read_text(encoding="utf-8"), (
        "The heartbeat must name the spawned session's id"
    )
    assert not events(case), "A planning Claude with a reused PID is not a live orchestrator"

    # If the new session exits too, spacing must still hold across store reopening.
    fake_processes.exit(spawned_pid)
    case.clock[0] += timedelta(seconds=119)
    reopened = StateStore(case.store.root, now=lambda: case.clock[0])
    assert ensure_orchestrator(case.root, reopened, case.config, now=case.clock[0]) is None
    assert len(commands) == len(pushes) == 1
    assert replacement.pid in fake_processes.live and not fake_processes.signals


def test_orch_spawn_liveness_other_repo_claude_allows_spawn_and_skill_requires_follow(
    liveness_case, fake_processes, tmp_path, monkeypatch, capsys,
):
    case, commands, pushes = liveness_case
    other_repo = tmp_path / "other-repo"
    other_repo.mkdir()
    other = fake_processes.add(
        43000, cwd=other_repo, start_time=case.clock[0].timestamp() - 3600,
        argv=("/offline/bin/claude", "/fa-orchestrator"),
    )

    assert ensure_orchestrator(case.root, case.store, case.config, now=case.clock[0]) == "background", (
        "A Claude session belonging to a different repository must not suppress this repository's spawn"
    )
    assert len(commands) == len(pushes) == 1
    assert not events(case), "Another repository's session is not a live orchestrator here"
    assert other.pid in fake_processes.live and not fake_processes.signals

    # Watch -> shell -> nearest plain Claude -> outer Claude. The nearest
    # argv[0] is claude even when psutil reports its name as claude.exe.
    start = case.clock[0].timestamp()
    outer = fake_processes.add(
        44000, cwd=case.root, start_time=start - 300,
        argv=("/offline/bin/claude", "/fa-orchestrator"),
    )
    nearest = fake_processes.add(
        fake_processes.grandparent_pid, ppid=outer.pid, cwd=case.root,
        start_time=start - 200, argv=("/offline/bin/claude",),
    )
    fake_processes.add(
        fake_processes.parent_pid, ppid=nearest.pid, cwd=case.root,
        start_time=start - 100, argv=("/bin/sh", "-c", "fa orch watch"),
    )
    fake_processes.add(
        fake_processes.self_pid, ppid=fake_processes.parent_pid, cwd=case.root,
        start_time=start - 10, argv=("fa", "orch", "watch"),
    )
    case.process_names[nearest.pid] = "claude.exe"
    main = bind_cli(case, monkeypatch)
    monkeypatch.setenv("FORAGENT_VIEW", "off")

    def stop_follow(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(orch.time, "sleep", stop_follow)
    observations = {}
    for mode, flags in (("one-shot", []), ("follow", ["--follow"])):
        case.clock[0] += timedelta(seconds=3)
        assert main(["orch", "watch", *flags, "--repo-root", str(case.root)]) == 0
        capsys.readouterr()
        heartbeat = json.loads((case.store.root / "orch-heartbeat.json").read_text(encoding="utf-8"))
        assert heartbeat["at"] == case.clock[0].isoformat()
        observations[mode] = heartbeat.get("session")

    skill = Path(__file__).resolve().parents[2] / "skills/fa-orchestrator/SKILL.md"
    observations["skill_follow"] = "fa orch watch --follow" in skill.read_text(encoding="utf-8")
    expected = {"pid": nearest.pid, "start_time": nearest.start_time}
    assert observations == {"one-shot": expected, "follow": expected, "skill_follow": True}, (
        "Both watch modes must record the nearest Claude ancestor's PID and start time, "
        "and the skill must name `fa orch watch --follow`; observed " + repr(observations)
    )
    assert not fake_processes.signals
