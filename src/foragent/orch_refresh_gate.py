"""The gate replaces a silent or aged seat holder with one fresh orchestrator (r1210).

``fa orch refresh --ready`` needs the outgoing session to write its handoff and
confirm. A session that fell silent never does, and before r1210 it kept the
seat for days. So the gate does the refresh itself when the seat holder's own
heartbeat (the heartbeat's active session is the holder's ``session_id``) has
been silent past ``orch_silent_alert_minutes``, or that session is older than
``orch_refresh_hours``: it writes a handoff from the last ``fa orch note``,
retires the holder from the seat (no signal) and opens exactly one session.

Silence starts at the later of the heartbeat's ``at`` and the holder's
``taken_at``, the same origin as ``orch_silent``. Only a session that has
beaten (its ``at`` is not before its ``started_at``; ``activate`` keeps the old
``at``) can be silent; a session still starting up is replaced only by age, so
a just-opened session is never replaced again at once.
A holder whose heartbeat names another session gives no attributable silence
and stays (the person still gets ``orch_silent``'s push).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from foragent import orch_refresh, orch_seat, orch_sessions
from foragent.config import Config
from foragent.orch_events import append_event
from foragent.state import StateStore


def path(store: StateStore) -> Path:
    """The gate's handoff; ``fa orch handoff``'s own note (``orch-handoff.md``) is left alone."""
    return store.root / "orch-handoff-gate.md"


def _at(value) -> datetime | None:
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo is not None else None


def due(store: StateStore, config: Config, now: datetime) -> dict | None:
    """``{holder, session, why}`` when the live seat holder must be replaced, else None."""
    lease, _reason = orch_seat.read(store)
    if lease is None:
        return None
    holder = lease["holder"]
    session = orch_sessions.active(store)
    if session is None or not holder.get("session_id") or holder["session_id"] != session["session_id"]:
        return None
    if orch_seat.alive(holder) is False:  # A dead or reused holder frees the seat; the normal spawn runs.
        return None
    beat, started = store.heartbeats()["orchestrator"], _at(session["started_at"])
    taken = _at(holder["taken_at"])
    minutes, hours = config.orch_silent_alert_minutes, config.orch_refresh_hours
    # Only a session that has beaten can fall silent; one still starting up waits for the age limit.
    if (minutes > 0 and beat is not None and started is not None and beat >= started
            and now - max(beat, taken or beat) >= timedelta(minutes=minutes)):
        return {"holder": holder, "session": session, "why": f"silent for {minutes}+ minutes"}
    if hours > 0 and started is not None and now - started >= timedelta(hours=hours):
        return {"holder": holder, "session": session, "why": f"older than {hours} hours"}
    return None


def _line(label: str, value) -> str:
    return f"- {label}: {value}" if value else ""


def write_handoff(store: StateStore, due_now: dict, now: datetime) -> Path:
    """Write the handoff from the last orchestrator note and append its ``handoff`` event."""
    note = store.orch_note() or {}
    old = due_now["session"]["session_id"]
    current = "\n".join(line for line in (
        _line("Now", note.get("now")), _line("Why", note.get("why")), _line("Next", note.get("next")),
        _line("Noted at", note.get("at"))) if line) or "- No orchestrator note was recorded."
    missing = "Not in the last note; read the sources listed in your first message."
    sections = {"Current work": current}
    body = [f"# Handoff written by the gate\n\nSession {old} was {due_now['why']}; the gate retired it "
            "from the seat without a signal and opened this session. Only Current work comes from "
            "its last `fa orch note`.\n"]
    for section in orch_refresh.SECTIONS:
        body.append(f"## {section}\n\n{sections.get(section, missing)}\n")
    target = path(store)
    with store._state_lock():
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text("\n".join(body), encoding="utf-8")
        temporary.replace(target)
        append_event(store, now, None, "handoff", f"gate handoff stored at {target}", locked=True,
                     path=str(target), session_id=old)
    return target


def replace(root: Path, store: StateStore, config: Config, now: datetime, due_now: dict) -> dict | None:
    """Inside the openers' lock: hand off, launch one session, then retire the holder and adopt it.

    Returns the plan of the session that opened, or None when the launch failed
    (the holder then keeps the seat; the spawn spacing paces the retry).
    """
    from foragent import orch_spawn  # Late: orch_spawn imports this module.

    note = write_handoff(store, due_now, now)
    old = due_now["session"]["session_id"]
    prompt = orch_refresh.first_prompt(config, note, old, root)
    planned = orch_spawn.plan_launch(root, store, config, now, prompt)
    if planned is None or not orch_spawn.launch(root, store, now, planned):
        orch_spawn.note_skip(store, "launch-unavailable" if planned is None else "launch-failed")
        return None
    orch_seat.retire(store, due_now["holder"], now)
    orch_spawn.adopt(store, planned, now)
    # A planned `fa orch refresh --ready` now needs a newer handoff than this replacement.
    append_event(store, now, None, "refresh", f"gate replaced session {old} ({due_now['why']}) -> "
                 f"{planned[3]['session_id']}", session_id=planned[3]["session_id"], previous_session_id=old,
                 handoff=str(note), by="gate")
    return planned
