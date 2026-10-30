"""Foragent presence in Claude Code: the short statusline segment (#754) and the
session-start summary with the first minute's big squirrel (#755).

Built from local state only (no GitHub call, no child process), because Claude
Code refreshes the statusline often.

Session start times live in ``$XDG_DATA_HOME/foragent/presence-sessions/<hash of
session_id>.json`` ({session_id, started_at}); the SessionStart hook is their only
writer. Claude's session ids are unique on the machine, so the record needs no
repository: a project without a repo key or GitHub origin still gets its squirrel.
"""

from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path

PERSON_LABELS = frozenset({"needs-human", "waiting-person"})
SESSIONS = "presence-sessions"
SQUIRREL_FOR = timedelta(seconds=60)
# The hook drops start records older than this; a squirrel never needs them again.
SESSION_KEEP = timedelta(days=1)
# A processing request untouched by a status change for longer than this is stuck.
STALL_AFTER = timedelta(minutes=60)


def project_root(start: Path) -> Path | None:
    """The nearest folder at or above ``start`` that holds .foragent.yml."""
    for folder in (start, *start.parents):
        if (folder / ".foragent.yml").is_file():
            return folder
    return None


def gate_merges(store) -> dict[tuple[int, str], str]:
    """Merge times from the gate's record_merge records, by (number, started_at).

    The one merge source (#754): merges outside the gate, in issue snapshots or PR
    listings, are not counted. record_merge writes runs/<n>.json at the merge;
    finish_run copies it into the history merge_times() reads, after the gate's
    GitHub calls. Reading both shows a merge at once; the key counts it once.
    """
    import json

    times = dict(store.merge_times())
    for path in (store.root / "runs").glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and isinstance(record.get("merged_at"), str) \
                and isinstance(record.get("started_at"), str) and isinstance(record.get("number"), int):
            times[record["number"], record["started_at"]] = record["merged_at"]
    return times


def counts(store, now: datetime) -> tuple[int, int, int]:
    """(working lanes, person's-turn issues, merges on the local calendar day of ``now``)."""
    from foragent.orch_inbox import waiting_issues

    lanes = {run.lane for run in store.active_runs()}
    turns = {row["number"] for row in store.local_issues()
             if row.get("state") == "open" and PERSON_LABELS & set(row.get("labels") or ())}
    turns |= waiting_issues(store)
    today = now.astimezone().date()
    merged = 0
    for (_, started), value in gate_merges(store).items():
        try:
            at, start = datetime.fromisoformat(value), datetime.fromisoformat(started)
        except ValueError:
            continue
        # A rerun's record carries the earlier execution's merge time; only a merge
        # after the execution started is its own (as in watch_sections.progress).
        merged += at >= start and at.astimezone().date() == today
    return len(lanes), len(turns), merged


def _project(cwd: Path):
    """(config, state store or None) for the Foragent project around ``cwd``, else None.

    Without a repo key, origin is read from git's files: presence runs no child process.
    The store is None when no repository can be named (no local state yet).
    """
    from foragent.config import load_config
    from foragent.git import origin_slug_from_files
    from foragent.state import StateStore

    root = project_root(cwd)
    if root is None:
        return None
    config = load_config(root)
    repo = config.repo or origin_slug_from_files(root)
    return config, (StateStore.for_repo(repo) if repo else None)


def _planning_session(cwd: Path, store) -> bool:
    """A planning session: no FORAGENT_ROLE at all (even empty), a nameable store, outside its worktrees."""
    import os

    if "FORAGENT_ROLE" in os.environ or store is None:
        return False
    return not cwd.resolve().is_relative_to((store.root / "worktrees").resolve())


def _parse_at(value) -> datetime | None:
    """A timezone-aware time from an ISO string, else None."""
    try:
        at = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return at if at.tzinfo else None


def _processing_since(row: dict):
    """When this processing request last moved into processing: a datetime, or None when unreadable.

    Only a note whose status differs from the one before it is a transition, so a
    repeated processing note does not reset the age. A malformed note (not an object,
    or missing status or at) is ignored on its own. Without a transition into
    processing, the creation time counts; updated_at never does.
    """
    selected = row.get("created_at")
    previous = "new"
    notes = row.get("notes")
    for note in notes if isinstance(notes, list) else ():
        if not isinstance(note, dict) or "status" not in note or "at" not in note:
            continue
        if note["status"] != previous and note["status"] == "processing":
            selected = note["at"]
        previous = note["status"]
    return _parse_at(selected)


def stalled_requests(store, now: datetime) -> tuple[int, int] | None:
    """(count, whole hours since the oldest) of processing requests stuck over STALL_AFTER, else None.

    Every source counts; a request whose selected time is unreadable is skipped alone.
    """
    from foragent.orch_inbox import requests

    since = [_processing_since(row) for row in requests(store) if row.get("status") == "processing"]
    stuck = [at for at in since if at is not None and now - at > STALL_AFTER]
    if not stuck:
        return None
    return len(stuck), int((now - min(stuck)) // timedelta(hours=1))


def renew_planning(cwd: Path) -> bool:
    """Keep this planning session's ``fa plan heartbeat`` fresh from the statusline (#970).

    Only a planning session renews: no FORAGENT_ROLE at all (even empty) and not
    inside the gate's worktrees. Only an existing heartbeat older than a minute is
    rewritten; nothing else, such as the session start record, is touched.
    """
    from foragent.orch_inbox import renew_plan_heartbeat

    try:
        project = _project(cwd)
        if project is None or not _planning_session(cwd, project[1]):
            return False
        return renew_plan_heartbeat(project[1])
    except Exception:  # The user's statusline must keep working whatever the local state holds.
        return False


def statusline_segment(cwd: Path, now: datetime) -> str | None:
    """The segment for a Foragent project, else None (and when the project config cannot be read)."""
    from foragent.i18n import text

    try:
        project = _project(cwd)
        if project is None or not project[0].presence["statusline"]:
            return None
        config, store = project
        # No local state yet (never run here, or no repo set): every count is zero.
        lanes, turns, merged = counts(store, now) if store and store.root.is_dir() else (0, 0, 0)
        segment = text("presence_statusline", config.language, lanes=lanes, turns=turns, merged=merged)
    except Exception:  # The user's statusline must keep working whatever the local state holds.
        return None
    try:
        # Only the planning session, which handles requests, is told about stuck ones.
        stalled = stalled_requests(store, now) if _planning_session(cwd, store) and store.root.is_dir() else None
        if stalled is None:
            return segment
        count, hours = stalled
        return segment + " · " + text("presence_stalled_requests", config.language, count=count, hours=hours)
    except Exception:  # An unreadable requests box leaves out only this fact.
        return segment


def _sessions() -> Path:
    from foragent.statusline import data_dir
    return data_dir() / SESSIONS


def _session_path(session_id: str) -> Path:
    # Hashed: a session id is outside input and must not name a path by itself.
    return _sessions() / (hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32] + ".json")


def _started_at(path: Path) -> datetime | None:
    try:
        at = datetime.fromisoformat(json.loads(path.read_text(encoding="utf-8"))["started_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return at if at.tzinfo else None


def record_session_start(session_id: str, now: datetime) -> None:
    """The one writer of session start times: record this one, drop day-old ones."""
    from foragent.statusline import _write_json

    folder = _sessions()
    folder.mkdir(parents=True, exist_ok=True)
    for path in folder.glob("*.json"):
        at = _started_at(path)
        if at is None or at < now - SESSION_KEEP:
            path.unlink(missing_ok=True)
    _write_json(_session_path(session_id), {"session_id": session_id, "started_at": now.isoformat()})


def session_start_message(cwd: Path, session_id: str | None, now: datetime) -> str | None:
    """Record the session start and return the one-line summary; None outside a project or when off.

    Merges count from the last report (fa brief --report's cursor, which this never moves),
    or the last 24 hours before the first report, as that report counts them.
    """
    from foragent import brief_report
    from foragent.i18n import text

    project = _project(cwd)
    if project is None or not project[0].presence["session_start"]:
        return None
    config, store = project
    if session_id:
        record_session_start(session_id, now)
    merged = turns = 0
    # No local state yet (never run here, or no repo to name it): the counts are zero.
    if store is not None and store.root.is_dir():
        since = brief_report.last_report(store, now) or now - brief_report.FIRST_WINDOW
        merged = sum(since < at <= now for at in brief_report._merges(store).values())
        turns = counts(store, now)[1]
    return text("presence_session_start", config.language, merged=merged, turns=turns)


def squirrel(cwd: Path, session_id: str | None, now: datetime) -> str | None:
    """The big squirrel's ANSI rows during a session's first minute, else None."""
    from foragent import watch_art

    try:
        project = _project(cwd)
        if not session_id or project is None or not project[0].presence["session_start"]:
            return None
        started = _started_at(_session_path(session_id))
        if started is None or not timedelta(0) <= now - started < SQUIRREL_FOR:
            return None
        return "\n".join(watch_art.ansi_rows(watch_art.ORCHESTRATOR, watch_art.PALETTE))
    except Exception:  # The user's statusline must keep working whatever the local state holds.
        return None


def append(output: bytes, segment: str | None) -> bytes:
    """Append ``segment`` to the last line of ``output``, keeping its final newline."""
    if not segment:
        return output
    body = output.rstrip(b"\r\n")
    separator = b"  " if body else b""
    return body + separator + segment.encode("utf-8") + output[len(body):]


def record_terminal(cwd: Path, now: datetime) -> None:
    """Best effort: record the planning session's live Orca terminal for the gate's readers.

    Runs whatever presence.session_start says. Skips silently without a handle, without
    orca on PATH, outside a project, without a nameable or existing state directory,
    and on any failure: the session that is starting must not notice.
    """
    import os
    import shutil

    from foragent import watch_open

    try:
        handle = os.environ.get("ORCA_TERMINAL_HANDLE")
        executable = shutil.which("orca") if handle else None
        if not executable:
            return
        project = _project(cwd)
        if project is None or project[1] is None or not project[1].root.is_dir():
            return
        watch_open.record_terminal(project[1], cwd, str(Path(executable).absolute()), handle, now)
    except Exception:
        return
