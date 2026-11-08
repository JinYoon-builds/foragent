"""Acceptance test for orch-self-replace (r1210, 11-08).

A silent seat holder is replaced by the gate with exactly one new orchestrator
session, the new session takes the seat with `fa orch watch --follow` while the
old process still runs, and the skill reaches planning only through fa channels.

Offline only: the process table is the shared fake table, launches are recorded
fakes, and no real signal or real Claude session is ever involved.
"""

from datetime import timedelta
import json
import os
from pathlib import Path
import re
import subprocess

import pytest

from foragent import orch
from foragent.state import StateStore
from test_heartbeats import heartbeats_offline  # noqa: F401 -- autouse isolation
from test_orch_refresh import OLD_SESSION, state_records
from test_orch_review_handoff import bind_cli, tick_context
from test_orch_seat import as_watcher, follow_once, read_seat, write_seat
from test_orch_spawn_liveness import launcher_returns_pid, liveness_case  # noqa: F401
from test_skills_autosync import cli_exit
from test_stop_whole_tree import events
from test_stop_whole_tree import fake_processes  # noqa: F401 -- offline autouse fixture

# The orchestrator's last note, written with `fa orch note` before it fell silent.
NOTE_NOW = "Watching PR #4312 for the r1210 seat fix; lane orch is paused for its schema change."
NOTE_WHY = "Jin asked to keep the orch lane paused until the seat fix merges."
NOTE_NEXT = "Resume lane orch after #4312 merges and re-arm the merge watch."
OLD_AGE = timedelta(hours=3)  # Below orch_refresh_hours (24): only the silence decides here.
DEFAULT_SILENCE = 30  # orch_silent_alert_minutes default


def claude_launches(launches):
    return [launch for launch in launches if Path(launch["argv"][0]).name == "claude"
            and "/fa-orchestrator" in launch["argv"]]


def session_id_of(argv):
    assert "--session-id" in argv, f"Every orchestrator launch names its own session id: {argv}"
    return argv[argv.index("--session-id") + 1]


def record_launches(monkeypatch):
    """Record argv and the effective environment of every non-git launch, then run the existing fake."""
    launches = []
    inner_run, inner_popen = subprocess.run, subprocess.Popen

    def note(argv, kwargs):
        parts = [str(part) for part in argv] if not isinstance(argv, str) else argv.split()
        if Path(parts[0]).name != "git":
            env = kwargs.get("env")
            launches.append({"argv": parts, "env": dict(env if env is not None else os.environ)})

    def run(argv, *args, **kwargs):
        note(argv, kwargs)
        return inner_run(argv, *args, **kwargs)

    def popen(argv, *args, **kwargs):
        note(argv, kwargs)
        return inner_popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "Popen", popen)
    return launches


def silent_holder(case, table, monkeypatch, capsys, *, argv, cwd):
    """A live seat holder whose heartbeat stopped; the gate runs inside that session's environment."""
    now = case.clock[0]
    old = table.add(43000, cwd=cwd, start_time=(now - OLD_AGE).timestamp(), argv=argv)
    holder = write_seat(case, old, taken_at=now - OLD_AGE, session_id=OLD_SESSION)
    transcript = case.root.parent / "old-transcript.jsonl"
    transcript.write_text("", encoding="utf-8")
    main = bind_cli(case, monkeypatch)
    capsys.readouterr()
    assert cli_exit(main, ["orch", "note", "--now", NOTE_NOW, "--why", NOTE_WHY, "--next", NOTE_NEXT,
                           "--repo-root", str(case.root)]) == 0
    capsys.readouterr()
    heartbeat = case.store.root / "orch-heartbeat.json"
    heartbeat.write_text(json.dumps({
        "at": (now - timedelta(minutes=DEFAULT_SILENCE - 1)).isoformat(), "session_id": OLD_SESSION,
        "transcript_path": str(transcript), "started_at": (now - OLD_AGE).isoformat(), "status": "active",
    }), encoding="utf-8")
    # The 11-07 gate was started from an orchestrator session and inherited its id.
    monkeypatch.setenv("FORAGENT_SESSION_ID", OLD_SESSION)
    monkeypatch.setenv("FORAGENT_ROLE", "orchestrator")
    return old, holder


def gate_tick(case):
    from foragent.scheduler import tick

    case.store = StateStore(case.store.root, now=lambda: case.clock[0])
    case.ctx.store = case.store
    tick(tick_context(case, start_runs=False))


def retired_search_records(root):
    """Every JSON object stored anywhere under the state root, whatever the file name or layout."""
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            yield from state_records(json.loads(text))
            continue
        except ValueError:
            pass
        for line in text.splitlines():
            try:
                yield from state_records(json.loads(line))
            except ValueError:
                continue


def handoff_texts(case):
    """Texts of the handoff the gate wrote: the stored note path and every `handoff` event's path."""
    from foragent import orch_refresh

    paths = {orch_refresh.handoff_path(case.store)}
    paths |= {Path(event["path"]) for event in events(case)
              if event.get("kind") == "handoff" and isinstance(event.get("path"), str)}
    return {str(path): path.read_text(encoding="utf-8", errors="replace") for path in paths if path.is_file()}


@pytest.mark.parametrize("old_argv", ["bg", "skill"])
def test_silent_live_holder_is_retired_with_a_handoff_and_one_new_session(
    liveness_case, fake_processes, monkeypatch, capsys, old_argv,
):
    case, commands, pushes = liveness_case
    argv, cwd = {
        "bg": (("/offline/bin/claude", "--bg"), case.root.parent / "session-host"),
        # A gate-spawned holder carries the skill argument and runs at the repository root.
        "skill": (("/offline/bin/claude", "--bg", "-n", f"orch: {case.root.name}", "/fa-orchestrator"),
                  case.root),
    }[old_argv]
    old, holder = silent_holder(case, fake_processes, monkeypatch, capsys, argv=argv, cwd=cwd)
    pid, start_time = launcher_returns_pid(case, fake_processes, monkeypatch)
    launches = record_launches(monkeypatch)

    gate_tick(case)
    assert claude_launches(launches) == [], (
        "Silence one minute below orch_silent_alert_minutes must not replace the holder"
    )
    assert read_seat(case)["holder"] == holder

    case.clock[0] += timedelta(minutes=2)  # Silent for 31 minutes; the process still runs.
    gate_tick(case)
    opened = claude_launches(launches)
    assert len(opened) == 1, (
        "The next gate tick after the silence threshold must open exactly one new orchestrator session; "
        f"launches={launches}"
    )
    new_id = session_id_of(opened[0]["argv"])
    assert new_id != OLD_SESSION

    texts = handoff_texts(case)
    written = [path for path, body in texts.items() if NOTE_NOW in body and NOTE_NEXT in body]
    assert written, f"The gate must write a handoff from the holder's last note; found {sorted(texts)}"
    first_message = " ".join(opened[0]["argv"])
    assert any(path in first_message for path in written) or NOTE_NOW in first_message, (
        "The new session must be given the handoff written from the last note"
    )

    seat = read_seat(case)["holder"]
    assert (seat["pid"], seat["start_time"]) != (old.pid, old.start_time), (
        "The silent holder must be retired from the seat"
    )
    assert (seat["pid"], seat["start_time"]) == (pid, start_time) and seat.get("session_id") == new_id
    heartbeat = json.loads((case.store.root / "orch-heartbeat.json").read_text(encoding="utf-8"))
    assert heartbeat.get("session_id") == new_id and heartbeat.get("status") == "active"
    # Archive filename, suffix and nesting are unrestricted (as in test_orch_refresh): read every
    # state file as one JSON document or as JSON lines, and look for the retired record anywhere.
    records = list(retired_search_records(case.store.root))
    assert any(row.get("session_id") == OLD_SESSION and row.get("status") == "retired"
               for row in records), "The silent session must be durably recorded as retired in heartbeat state"
    assert old.pid in fake_processes.live and not fake_processes.signals, (
        "The old process is retired, never signalled"
    )

    # Exactly one: later ticks past the spawn spacing open nothing more.
    for minutes in (3, 10):
        case.clock[0] += timedelta(minutes=minutes)
        gate_tick(case)
        assert len(claude_launches(launches)) == 1, (
            f"Only one orchestrator session may be opened for one silence; launches={launches}"
        )
    assert not fake_processes.signals


@pytest.mark.parametrize("launcher", ["identified", "unidentified"])
def test_new_session_takes_the_seat_from_the_live_silent_holder_and_is_alive(
    liveness_case, fake_processes, monkeypatch, capsys, launcher,
):
    case, commands, pushes = liveness_case
    old, holder = silent_holder(case, fake_processes, monkeypatch, capsys,
                                argv=("/offline/bin/claude", "--bg"), cwd=case.root.parent / "session-host")
    if launcher == "identified":
        pid, start_time = launcher_returns_pid(case, fake_processes, monkeypatch)
    launches = record_launches(monkeypatch)
    case.clock[0] += timedelta(minutes=2)  # Silent for 31 minutes.
    gate_tick(case)
    opened = claude_launches(launches)
    assert len(opened) == 1, f"The silent holder must be replaced by one new session; launches={launches}"
    new_id = session_id_of(opened[0]["argv"])
    env_id = opened[0]["env"].get("FORAGENT_SESSION_ID")
    assert env_id == new_id != OLD_SESSION, (
        "The new session must run with its own FORAGENT_SESSION_ID, not the parent's: "
        f"env={env_id!r} argv={new_id!r}"
    )

    if launcher == "identified":
        session = fake_processes.process(pid)
    else:
        # `claude --bg` returned without a readable launcher identity; the session runs on its own.
        session = fake_processes.add(
            89000, cwd=case.root, start_time=case.clock[0].timestamp(),
            argv=("/offline/bin/claude", "--bg", "-n", f"orch: {case.root.name}", "/fa-orchestrator"),
        )
    # The new session is still starting up (no heartbeat yet); a tick past the spawn spacing opens nothing.
    case.clock[0] += timedelta(minutes=3)
    gate_tick(case)
    assert len(claude_launches(launches)) == 1, (
        f"Only one orchestrator session may be active at a time; launches={launches}"
    )
    as_watcher(case, fake_processes, monkeypatch, session_id=env_id, process=session)
    case.clock[0] += timedelta(seconds=30)
    code, output, sleeps = follow_once(case, monkeypatch, capsys)
    assert code == 0 and sleeps == [1], (
        "The new session's `fa orch watch --follow` must take the seat although the old process "
        f"is still alive: {output}"
    )
    seat = read_seat(case)["holder"]
    assert (seat["pid"], seat["start_time"]) == (session.pid, session.start_time)
    assert seat.get("session_id") == new_id
    assert old.pid in fake_processes.live, "The old process keeps running; it lost the seat only"

    main = bind_cli(case, monkeypatch)
    capsys.readouterr()
    assert cli_exit(main, ["orch", "alive", "--json", "--repo-root", str(case.root)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["alive"] is True, (
        f"`fa orch alive` must count the new session's heartbeat: {report}"
    )
    assert not fake_processes.signals


def _sentences(text):
    return [part for part in re.split(r"(?<=[.!?])\s+", " ".join(text.split())) if part]


def test_skill_reaches_planning_only_through_fa_channels_not_sendmessage():
    root = Path(orch.__file__).resolve().parents[2] / "skills/fa-orchestrator"
    files = sorted(root.rglob("*.md"))
    assert root / "SKILL.md" in files
    told = []
    for path in files:
        for sentence in _sentences(path.read_text(encoding="utf-8")):
            if "SendMessage" in sentence and not re.search(r"\b(?:never|not|don't|no)\b", sentence, re.I):
                told.append(f"{path.relative_to(root)}: {sentence}")
    assert told == [], (
        "The fa-orchestrator skill must not tell the orchestrator to send planning messages with "
        "SendMessage:\n" + "\n".join(told)
    )
    skill = " ".join((root / "SKILL.md").read_text(encoding="utf-8").split())
    named = False
    for match in re.finditer(r"planning", skill, re.I):
        window = skill[max(0, match.start() - 400):match.end() + 400]
        if ("fa orch ask" in window and "fa orch request-status" in window
                and re.search(r"\b(?:only|channel)\b", window, re.I)):
            named = True
            break
    assert named, (
        "SKILL.md must name `fa orch ask` and `fa orch request-status` as the only channel to the planning session"
    )
