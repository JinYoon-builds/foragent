"""Boundaries of the planned orchestrator refresh (#413) beyond the acceptance file."""

from datetime import UTC, datetime, timedelta
import json
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


def test_gate_tmux_spawn_writes_the_descriptor_with_an_env_prefix(setup, monkeypatch):
    store, repo, calls = setup
    monkeypatch.setenv("TMUX", "/tmp/tmux")
    store.save_approval(1, body="b", body_sha="s", acceptance_blobs={}, approved_by="human",
                        approved_at=NOW.isoformat())
    assert orch_spawn.ensure_orchestrator(repo, store, Config(repo="o/r"), now=NOW) == "tmux"
    command = shlex.split(calls[0][0][-1])
    record = _heartbeat(store)
    assert command[:3] == ["env", "FORAGENT_ROLE=orchestrator", f"FORAGENT_SESSION_ID={record['session_id']}"]
    assert command[command.index("--session-id") + 1] == record["session_id"] and record["status"] == "active"


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


def test_background_gate_spawn_frees_the_slot_and_the_new_session_claims_it(setup, monkeypatch):
    from foragent.orch_spawn_launch import transcript_path

    store, repo, _calls = setup
    _approve(store)
    orch_sessions.activate(store, OLD, NOW - timedelta(hours=2))  # a dead session's record
    assert orch_spawn.ensure_orchestrator(repo, store, Config(repo="o/r"), now=NOW) == "background"
    record = _heartbeat(store)
    assert "session_id" not in record and record["previous"]["session_id"] == "old-id"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "bg-id")  # what the new session's watch sees
    assert orch_sessions.claim_own(store, repo, NOW + timedelta(seconds=3)) is True
    assert orch_sessions.active(store) == {"session_id": "bg-id", "transcript_path": transcript_path(repo, "bg-id"),
                                           "started_at": (NOW + timedelta(seconds=3)).isoformat()}
    report = orch_refresh.due(store, Config(repo="o/r"), NOW + timedelta(seconds=4))
    assert report["session_id"] == "bg-id"


def test_claim_never_takes_a_held_slot_or_returns_a_retired_session(setup, monkeypatch):
    store, repo, _calls = setup
    orch_sessions.activate(store, OLD, NOW)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "other-id")
    assert orch_sessions.claim_own(store, repo, NOW) is False
    orch_sessions.retire_active(store, NOW)
    monkeypatch.setenv("FORAGENT_SESSION_ID", "old-id")  # Foragent's name wins over Claude's
    assert orch_sessions.claim_own(store, repo, NOW) is False
    assert orch_sessions.active(store) is None


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
    activate = orch_sessions.activate
    monkeypatch.setattr(orch_sessions, "activate",
                        lambda *args, **kwargs: seen.append(_lock_held(store)) or activate(*args, **kwargs))
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
