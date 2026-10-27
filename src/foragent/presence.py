"""Foragent presence in Claude Code: the short statusline segment (#754) and the
session-start summary with the first minute's big squirrel (#755).

Built from local state only (no GitHub call, no child process), because Claude
Code refreshes the statusline often.

Session start times live in ``<state>/sessions/<hash of session_id>.json``
({session_id, started_at}); the SessionStart hook is their only writer.
"""

from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path

PERSON_LABELS = frozenset({"needs-human", "waiting-person"})
SESSIONS = "sessions"
SQUIRREL_FOR = timedelta(seconds=60)
# The hook drops start records older than this; a squirrel never needs them again.
SESSION_KEEP = timedelta(days=1)


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
        return text("presence_statusline", config.language, lanes=lanes, turns=turns, merged=merged)
    except Exception:  # The user's statusline must keep working whatever the local state holds.
        return None


def _session_path(store, session_id: str) -> Path:
    # Hashed: a session id is outside input and must not name a path by itself.
    return store.root / SESSIONS / (hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32] + ".json")


def _started_at(path: Path) -> datetime | None:
    try:
        at = datetime.fromisoformat(json.loads(path.read_text(encoding="utf-8"))["started_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return at if at.tzinfo else None


def record_session_start(store, session_id: str, now: datetime) -> None:
    """The one writer of session start times: record this one, drop day-old ones."""
    folder = store.root / SESSIONS
    folder.mkdir(parents=True, exist_ok=True)
    for path in folder.glob("*.json"):
        at = _started_at(path)
        if at is None or at < now - SESSION_KEEP:
            path.unlink(missing_ok=True)
    store._write_json(_session_path(store, session_id), {"session_id": session_id, "started_at": now.isoformat()})


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
    merged = turns = 0
    if store is not None:
        if session_id:
            record_session_start(store, session_id, now)
        if store.root.is_dir():
            since = brief_report.last_report(store, now) or now - brief_report.FIRST_WINDOW
            merged = sum(since < at <= now for at in brief_report._merges(store).values())
            turns = counts(store, now)[1]
    return text("presence_session_start", config.language, merged=merged, turns=turns)


def squirrel(cwd: Path, session_id: str | None, now: datetime) -> str | None:
    """The big squirrel's ANSI rows during a session's first minute, else None."""
    from foragent import watch_art

    try:
        project = _project(cwd)
        if not session_id or project is None or not project[0].presence["session_start"] or project[1] is None:
            return None
        started = _started_at(_session_path(project[1], session_id))
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
