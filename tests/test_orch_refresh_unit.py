"""Boundaries of the planned orchestrator refresh (#413) beyond the acceptance file."""

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import shlex
import shutil
import subprocess

import pytest

from foragent import notify, orch_inbox, orch_refresh, orch_sessions, orch_spawn
from foragent.config import Config
from foragent.state import StateStore

NOW = datetime(2026, 10, 29, 12, tzinfo=UTC)
OLD = {"session_id": "old-id", "transcript_path": "/t/old-id.jsonl", "started_at": NOW.isoformat()}
NOTE = "".join(f"## {section}\n\nnone\n\n" for section in orch_refresh.SECTIONS)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    calls = []

    def run(argv, *args, **kwargs):
        calls.append((argv, kwargs.get("env") or {}))
        return subprocess.CompletedProcess(argv, 0)

    for name in ("FORAGENT_ORCH_SPAWN", "ORCA_TERMINAL_HANDLE", "TMUX", "CLAUDE_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(shutil, "which", lambda name: f"/offline/bin/{name}" if name in ("claude", "tmux") else None)
    monkeypatch.setattr(notify, "send", lambda *args, **kwargs: True)
    repo = tmp_path / "re po"
    repo.mkdir()
    return store, repo.resolve(), calls


def _heartbeat(store):
    return json.loads((store.root / "orch-heartbeat.json").read_text(encoding="utf-8"))


def test_heartbeat_keeps_session_fields_and_ignores_a_retired_session(setup):
    store, _repo, _calls = setup
    orch_sessions.activate(store, OLD, NOW)
    later = NOW + timedelta(seconds=5)
    store.write_heartbeat("orchestrator", later)  # orch watch without an id
    assert _heartbeat(store)["at"] == later.isoformat() and _heartbeat(store)["session_id"] == "old-id"
    store.write_heartbeat("orchestrator", later + timedelta(seconds=1), session_id="old-id")
    assert _heartbeat(store)["at"] == (later + timedelta(seconds=1)).isoformat()
    before = _heartbeat(store)
    store.write_heartbeat("orchestrator", later + timedelta(seconds=9), session_id="retired-id")
    assert _heartbeat(store) == before


def test_activate_retires_the_old_session_into_previous_and_history(setup):
    store, _repo, _calls = setup
    orch_sessions.activate(store, OLD, NOW)
    new = {**OLD, "session_id": "new-id"}
    retired = orch_sessions.activate(store, new, NOW + timedelta(seconds=1))
    record = _heartbeat(store)
    assert record["session_id"] == "new-id" and record["status"] == "active"
    assert record["previous"] == retired and retired["session_id"] == "old-id" and retired["status"] == "retired"
    history = (store.root / "orch-sessions.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["session_id"] for line in history] == ["old-id"]


def test_spawn_names_the_session_passes_prompt_and_role_env(setup, monkeypatch):
    store, repo, calls = setup
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/cfg")
    session = orch_spawn.spawn_orchestrator(repo, store, Config(repo="o/r"), prompt="Read first.", now=NOW)
    (argv, env), = calls
    assert argv[:2] == ["/offline/bin/claude", "--bg"]
    assert argv[-4:] == ["--session-id", session["session_id"], "/fa-orchestrator", "Read first."]
    assert env["FORAGENT_ROLE"] == "orchestrator" and env["FORAGENT_SESSION_ID"] == session["session_id"]
    project = "".join(char if char.isalnum() else "-" for char in str(repo))
    assert session["transcript_path"] == f"/cfg/projects/{project}/{session['session_id']}.jsonl"
    assert session["started_at"] == NOW.isoformat()
    assert not (store.root / "orch-heartbeat.json").exists(), "refresh, not the spawn, activates the session"


def _ready(store, repo):
    source = repo / "NOTE.md"
    source.write_text(NOTE, encoding="utf-8")
    orch_sessions.activate(store, OLD, NOW)
    orch_refresh.accept_handoff(store, Config(repo="o/r"), source, NOW)


def _events(store, kind):
    path = store.root / "orch-events.jsonl"
    return [row for line in path.read_text(encoding="utf-8").splitlines() if (row := json.loads(line))["kind"] == kind]


def test_failed_spawn_keeps_the_old_session_and_the_handoff(setup, monkeypatch):
    store, repo, _calls = setup
    _ready(store, repo)
    monkeypatch.setenv("FORAGENT_ORCH_SPAWN", "off")
    later = NOW + timedelta(seconds=1)
    with pytest.raises(orch_refresh.RefreshRefused, match="stays active"):
        orch_refresh.refresh(repo, store, Config(repo="o/r"), later)
    assert _heartbeat(store)["session_id"] == "old-id"
    assert _events(store, "refresh") == [] and len(_events(store, "refresh-failed")) == 1
    monkeypatch.delenv("FORAGENT_ORCH_SPAWN")
    orch_refresh.refresh(repo, store, Config(repo="o/r"), later)  # the same note is still usable
    assert _heartbeat(store)["session_id"] != "old-id" and len(_events(store, "refresh")) == 1


@pytest.mark.parametrize("answered_seconds,blocked", [(None, True), (300, True), (301, False)])
def test_open_or_recently_answered_questions_block_refresh(setup, answered_seconds, blocked):
    store, repo, _calls = setup
    _ready(store, repo)
    question = orch_inbox.ask(store, "Which order next?", [], None)
    now = NOW
    if answered_seconds is not None:
        orch_inbox.answer(store, question["id"], "The docs order.")
        now = NOW + timedelta(seconds=answered_seconds)
    if blocked:
        with pytest.raises(orch_refresh.RefreshRefused, match=rf"{question['id']}.*inbox"):
            orch_refresh.refresh(repo, store, Config(repo="o/r"), now)
        assert _heartbeat(store)["session_id"] == "old-id"
    else:
        orch_refresh.refresh(repo, store, Config(repo="o/r"), now)
        assert _heartbeat(store)["session_id"] != "old-id"


def test_due_without_a_transcript_is_not_due_with_a_reason(setup):
    store, _repo, _calls = setup
    orch_sessions.activate(store, OLD, NOW)
    report = orch_refresh.due(store, Config(repo="o/r"), NOW + timedelta(hours=30))
    assert report["due"] is False and "transcript" in report["reason"]
    assert orch_refresh.due(StateStore(store.root.parent / "empty"), Config(), NOW)["due"] is False


def test_first_prompt_names_every_lane_queue():
    prompt = orch_refresh.first_prompt(Config(lanes={"orch": {}, "view": {}}), "/s/orch-handoff.md", "old")
    for lane in ("orch", "view", "main"):
        assert f"`fa orch queue {lane}`" in prompt


def test_compactions_count_only_the_active_sessions_boundaries(tmp_path):
    rows = [{"type": "system", "subtype": "compact_boundary", "sessionId": session}
            for session in ("old-id", "other-id", "old-id")]
    path = tmp_path / "t.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows) + "not json compact_boundary\n")
    assert orch_refresh.compactions(path, "old-id") == 2


def _approve(store):
    store.save_approval(1, body="b", body_sha="s", acceptance_blobs={}, approved_by="human",
                        approved_at=NOW.isoformat())


@pytest.mark.parametrize("screen", ["background", "tmux", "orca"])
@pytest.mark.parametrize("explicit", [False, True], ids=["gate", "ensure"])
def test_every_launch_gets_its_own_new_session_id_and_records_it(setup, monkeypatch, screen, explicit):
    store, repo, calls = setup
    _approve(store)
    monkeypatch.setenv("FORAGENT_SESSION_ID", "launcher-id")  # e.g. a gate started from a session
    if screen == "tmux":
        monkeypatch.setenv("TMUX", "/tmp/tmux")
    if screen == "orca":
        monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "h")
        monkeypatch.setattr(shutil, "which", lambda name: f"/offline/bin/{name}" if name in ("claude", "orca") else None)
    orch_sessions.activate(store, OLD, NOW - timedelta(hours=2))  # a dead session's record
    before = (store.root / "orch-heartbeat.json").read_bytes()
    assert orch_spawn.ensure_orchestrator(repo, store, Config(repo="o/r"), now=NOW, spacing=not explicit) == screen
    (argv, env), = calls
    record = _heartbeat(store)
    new_id = record["session_id"]
    assert new_id not in ("launcher-id", "old-id") and record["status"] == "active", "written at once"
    assert record["previous"]["session_id"] == "old-id" and record["previous"]["status"] == "retired"
    assert env["FORAGENT_SESSION_ID"] == new_id and env["FORAGENT_ROLE"] == "orchestrator"
    command = argv if screen == "background" else shlex.split(argv[-1])
    assert command[command.index("--session-id") + 1] == new_id
    if screen != "background":  # the terminal app does not inherit the launcher's environment
        assert command[:3] == ["env", "FORAGENT_ROLE=orchestrator", f"FORAGENT_SESSION_ID={new_id}"]
    assert orch_refresh.due(store, Config(repo="o/r"), NOW)["session_id"] == new_id
    later = NOW + timedelta(seconds=5)
    after_launch = (store.root / "orch-heartbeat.json").read_bytes()
    assert after_launch != before
    store.write_heartbeat("orchestrator", later, session_id="old-id")  # the dead session is ignored
    assert (store.root / "orch-heartbeat.json").read_bytes() == after_launch
    store.write_heartbeat("orchestrator", later, session_id=new_id)  # the new session's first write
    record = _heartbeat(store)
    assert record["session_id"] == new_id and record["status"] == "active" and record["at"] == later.isoformat()
    history = [json.loads(line)["session_id"]
               for line in (store.root / "orch-sessions.jsonl").read_text(encoding="utf-8").splitlines()]
    assert history == ["old-id"]
    store.write_heartbeat("orchestrator", later + timedelta(seconds=5), session_id=new_id)
    assert len((store.root / "orch-sessions.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_refresh_after_a_gate_spawn_retires_the_spawned_session_too(setup, monkeypatch):
    store, repo, _calls = setup
    _approve(store)
    _ready(store, repo)  # OLD active in the heartbeat, a fresh note stored
    assert orch_spawn.ensure_orchestrator(repo, store, Config(repo="o/r"), now=NOW + timedelta(seconds=1))
    spawned = orch_sessions.active(store)["session_id"]
    orch_refresh.refresh(repo, store, Config(repo="o/r"), NOW + timedelta(seconds=2))
    history = [json.loads(line)["session_id"]
               for line in (store.root / "orch-sessions.jsonl").read_text(encoding="utf-8").splitlines()]
    assert history == ["old-id", spawned] and _heartbeat(store)["previous"]["session_id"] == spawned
    assert orch_sessions.active(store)["session_id"] not in ("old-id", spawned)


def test_refresh_spawn_overrides_an_inherited_session_id(setup, monkeypatch):
    store, repo, calls = setup
    monkeypatch.setenv("FORAGENT_SESSION_ID", "old-id")  # refresh runs inside the outgoing session
    session = orch_spawn.spawn_orchestrator(repo, store, Config(repo="o/r"), prompt="Read first.", now=NOW)
    assert calls[0][1]["FORAGENT_SESSION_ID"] == session["session_id"] != "old-id"


def _lock_held(store) -> bool:
    import fcntl
    import os

    fd = os.open(store.root / "watch-open.lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


@pytest.mark.parametrize("explicit", [False, True], ids=["gate", "ensure"])
def test_spawned_session_is_recorded_inside_the_openers_lock(setup, monkeypatch, explicit):
    store, repo, _calls = setup
    monkeypatch.setenv("TMUX", "/tmp/tmux")
    _approve(store)
    seen = []
    record = orch_sessions.activate
    monkeypatch.setattr(orch_sessions, "activate",
                        lambda *args, **kwargs: seen.append(_lock_held(store)) or record(*args, **kwargs))
    assert orch_spawn.ensure_orchestrator(repo, store, Config(repo="o/r"), now=NOW, spacing=not explicit) == "tmux"
    assert seen and all(seen), seen


def test_concurrent_refreshes_consume_one_handoff_once(setup, monkeypatch):
    import threading

    store, repo, _calls = setup
    _ready(store, repo)
    started, spawned = threading.Event(), []

    def slow_spawn(repo_root, store, config, *, prompt, now):
        spawned.append(prompt)
        started.set()
        threading.Event().wait(0.3)  # the second refresh arrives while this one launches
        return {"session_id": f"new-{len(spawned)}", "transcript_path": "/t/n.jsonl", "started_at": now.isoformat()}

    monkeypatch.setattr(orch_spawn, "spawn_orchestrator", slow_spawn)
    results = []

    def attempt():
        try:
            results.append(orch_refresh.refresh(repo, store, Config(repo="o/r"), NOW + timedelta(seconds=1)))
        except orch_refresh.RefreshRefused as exc:
            results.append(exc)

    first = threading.Thread(target=attempt)
    first.start()
    started.wait(5)
    second = threading.Thread(target=attempt)
    second.start()
    first.join(5)
    second.join(5)
    assert len(spawned) == 1 and len(_events(store, "refresh")) == 1
    assert sum(isinstance(result, orch_refresh.RefreshRefused) for result in results) == 1


def test_new_command_help_follows_the_repository_language(tmp_path, monkeypatch, capsys):
    from foragent.cli import main

    (tmp_path / ".foragent.yml").write_text("repo: o/r\nlanguage: ko\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path.parent)
    with pytest.raises(SystemExit):
        main(["orch", "refresh", "--help", "--repo-root", str(tmp_path)])
    assert "인계 노트를 썼고" in capsys.readouterr().out
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        main(["orch", "--help"])
    out = capsys.readouterr().out
    assert "새 오케스트레이터 세션을 열어요" in out and "교체 기준과 비교해" in out and "인계 노트를 확인하고" in out


def _history(store):
    path = store.root / "orch-sessions.jsonl"
    return [json.loads(line)["session_id"] for line in path.read_text(encoding="utf-8").splitlines()] \
        if path.exists() else []


def _spawn(store, repo, at, *, fail=False, monkeypatch=None):
    if fail:
        def broken(argv, *args, **kwargs):
            raise subprocess.CalledProcessError(1, argv)
        monkeypatch.setattr(subprocess, "run", broken)
    return orch_spawn.ensure_orchestrator(repo, store, Config(repo="o/r"), now=at)


def test_failed_second_launch_keeps_the_first_spawned_session_active(setup, monkeypatch):
    store, repo, _calls = setup
    _approve(store)
    orch_sessions.activate(store, OLD, NOW - timedelta(hours=2))
    _spawn(store, repo, NOW)
    first = orch_sessions.active(store)["session_id"]
    assert _spawn(store, repo, NOW + timedelta(minutes=3), fail=True, monkeypatch=monkeypatch) is None
    assert orch_sessions.active(store)["session_id"] == first, "a failed launch must not revive the older session"
    assert _history(store) == ["old-id"]


def test_second_launch_retires_the_first_spawned_session_once(setup):
    store, repo, _calls = setup
    _approve(store)
    orch_sessions.activate(store, OLD, NOW - timedelta(hours=2))
    _spawn(store, repo, NOW)
    first = orch_sessions.active(store)["session_id"]
    _spawn(store, repo, NOW + timedelta(minutes=3))
    second = orch_sessions.active(store)["session_id"]
    assert second != first and _history(store) == ["old-id", first]
    store.write_heartbeat("orchestrator", NOW + timedelta(minutes=4), session_id=first)  # retired: ignored
    assert orch_sessions.active(store)["session_id"] == second
    store.write_heartbeat("orchestrator", NOW + timedelta(minutes=4), session_id=second)
    assert _history(store) == ["old-id", first] and _heartbeat(store)["session_id"] == second


def test_a_retired_spawned_session_never_returns_even_on_a_timestamp_tie(setup):
    store, repo, _calls = setup
    _approve(store)
    _ready(store, repo)
    _spawn(store, repo, NOW)
    spawned = orch_sessions.active(store)["session_id"]
    orch_refresh.refresh(repo, store, Config(repo="o/r"), NOW)  # same instant as the gate's launch
    assert spawned in _history(store)
    fresh = _heartbeat(store)["session_id"]
    assert fresh != spawned and orch_sessions.active(store)["session_id"] == fresh
