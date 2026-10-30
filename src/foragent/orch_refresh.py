"""Planned orchestrator refresh (#413): measure, hand off, then open a fresh session.

``fa orch refresh-due`` reports facts only (compactions and age of the active
session against ``orch_refresh_compactions`` and ``orch_refresh_hours``) and
writes nothing. Whether now is a good moment stays the orchestrator's call.
``fa orch handoff --file`` checks the note's sections and stores a verbatim copy
as ``<state>/orch-handoff.md`` with a ``handoff`` event. ``fa orch refresh
--ready`` needs a stored, still complete note accepted after the last
``refresh`` event and no person waiting in the inbox; it then opens a fresh
session through ``orch_spawn.spawn_orchestrator``, makes it active in the
heartbeat and appends one ``refresh`` event. The old session gets no signal:
the command tells it to end itself.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
import re

from foragent import orch_inbox, orch_sessions, orch_spawn, watch_open
from foragent.config import Config
from foragent.i18n import text
from foragent.labels import lane_order
from foragent.orch_events import append_event
from foragent.state import StateStore

ACTIONS = ("refresh-due", "handoff", "refresh")
SECTIONS = ("Current work", "Watches", "Open promises", "Temporary settings",
            "Queued orders not yet published", "Pending decisions for the person", "Peers",
            "Recent incidents")
# A person message this recent, and not done, means a conversation may be going on.
QUIET = timedelta(minutes=5)
_HEADING = re.compile(r"^(#{1,2})\s+(.*?)\s*#*\s*$")


class RefreshRefused(orch_inbox.InboxError):
    """A refused refresh or handoff; the CLI prints it and exits 1."""


def handoff_path(store: StateStore) -> Path:
    return store.root / "orch-handoff.md"


def compactions(path: Path, session_id: str) -> int:
    """Top-level ``compact_boundary`` system records of this session in a transcript."""
    count = 0
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if "compact_boundary" not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if (isinstance(row, dict) and row.get("type") == "system" and row.get("subtype") == "compact_boundary"
                    and row.get("sessionId") == session_id):
                count += 1
    return count


def due(store: StateStore, config: Config, now: datetime) -> dict:
    """The refresh-due facts; reads the heartbeat and transcript, writes nothing."""
    language = config.language
    thresholds = {"orch_refresh_compactions": config.orch_refresh_compactions,
                  "orch_refresh_hours": config.orch_refresh_hours}
    report = {"session_id": None, "transcript_path": None, "compactions": None, "age_seconds": None,
              "thresholds": thresholds, "due": False}
    if not orch_sessions.heartbeat_path(store).is_file():
        return {**report, "reason": text("orch_refresh_no_heartbeat", language)}
    session = orch_sessions.active(store)
    try:
        started = datetime.fromisoformat(session["started_at"]) if session else None
    except ValueError:
        started = None
    if session is None or started is None or started.tzinfo is None:
        return {**report, "reason": text("orch_refresh_no_session", language)}
    report.update(session_id=session["session_id"], transcript_path=session["transcript_path"])
    report["age_seconds"] = age = int((now - started).total_seconds())
    path = Path(session["transcript_path"])
    try:
        count = compactions(path, session["session_id"])
    except OSError:
        return {**report, "reason": text("orch_refresh_no_transcript", language, path=path)}
    report["compactions"] = count
    limit, limit_hours = config.orch_refresh_compactions, config.orch_refresh_hours
    by_count, by_age = count >= limit, age >= limit_hours * 3600
    values = {"count": count, "limit": limit, "hours": round(age / 3600, 1), "limit_hours": limit_hours}
    if by_count and by_age:
        reason = text("orch_refresh_due_both", language, **values)
    elif by_count:
        reason = text("orch_refresh_due_compactions", language, **values)
    elif by_age:
        reason = text("orch_refresh_due_age", language, **{**values, "limit": limit_hours})
    else:
        reason = text("orch_refresh_not_due", language, **values)
    return {**report, "due": by_count or by_age, "reason": reason}


def note_defects(note: str) -> list[str]:
    """Required sections that are missing or empty, each as ``<heading> (missing|empty)``."""
    bodies: dict[str, list[str]] = {}
    current = None
    for line in note.splitlines():
        if match := _HEADING.match(line):
            name = match.group(2).casefold()
            current = name if match.group(1) == "##" else None
            bodies.setdefault(name, []) if current else None
        elif current is not None:
            bodies[current].append(line)
    defects = []
    for section in SECTIONS:
        lines = bodies.get(section.casefold())
        if lines is None:
            defects.append(f"{section} (missing)")
        elif not "".join(lines).strip():
            defects.append(f"{section} (empty)")
    return defects


def accept_handoff(store: StateStore, config: Config, source: Path, now: datetime) -> Path:
    """Check ``source`` and store it verbatim; a refused note writes nothing."""
    language = config.language
    try:
        data = source.read_bytes()
    except OSError as exc:
        raise RefreshRefused(text("orch_handoff_missing_file", language, path=source, error=exc)) from None
    if defects := note_defects(data.decode("utf-8", errors="replace")):
        raise RefreshRefused(text("orch_handoff_invalid", language, sections="; ".join(defects)))
    path = handoff_path(store)
    with store._state_lock():
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_bytes(data)
        temporary.replace(path)
        append_event(store, now, None, "handoff", f"handoff note stored at {path}", locked=True,
                     path=str(path))
    return path


def _events(store: StateStore, kind: str) -> list[datetime]:
    path = store.root / "orch-events.jsonl"
    if not path.exists():
        return []
    times = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
            if isinstance(row, dict) and row.get("kind") == kind:
                times.append(datetime.fromisoformat(row["at"]))
        except (ValueError, KeyError, TypeError):
            continue
    return times


def _fresh_handoff(store: StateStore, language: str) -> Path:
    """The stored note, accepted after the last refresh and still complete; else refuse."""
    handoffs, refreshes = _events(store, "handoff"), _events(store, "refresh")
    path = handoff_path(store)
    if not handoffs or not path.is_file() or (refreshes and max(handoffs) <= max(refreshes)):
        raise RefreshRefused(text("orch_refresh_no_handoff", language))
    if defects := note_defects(path.read_text(encoding="utf-8", errors="replace")):
        raise RefreshRefused(text("orch_refresh_bad_handoff", language, sections="; ".join(defects)))
    return path


def _recent(stamp, now: datetime) -> bool:
    try:
        return now - datetime.fromisoformat(stamp) <= QUIET
    except (TypeError, ValueError):
        return False


def _quiet(store: StateStore, now: datetime, language: str) -> None:
    """Refuse while a person request is pending and recent, or a question is open or just answered."""
    minutes = int(QUIET.total_seconds() // 60)
    people = [row["id"] for row in orch_inbox.requests(store, pending=True)
              if row.get("from") == "person" and _recent(row.get("created_at"), now)]
    if people:
        raise RefreshRefused(text("orch_refresh_person", language, minutes=minutes, ids=", ".join(people)))
    questions = [row["id"] for row in orch_inbox.questions(store, include_closed=True)
                 if row.get("status") == "open" or _recent(row.get("answered_at"), now)]
    if questions:
        raise RefreshRefused(text("orch_refresh_questions", language, minutes=minutes, ids=", ".join(questions)))


def first_prompt(config: Config, note: Path, old: str | None) -> str:
    """The fresh session's first message: the note and the sources to read before acting."""
    lanes = sorted({*config.lanes, "main"}, key=lane_order)
    queues = "\n".join(f"   - `fa orch queue {lane}`" for lane in lanes)
    replacing = f" replacing session {old}" if old else ""
    return (
        f"You are a fresh orchestrator session{replacing}. Read all of these before you act:\n"
        f"1. The handoff note: {note}\n"
        "2. docs/decisions.md\n"
        "3. `fa orch plan`\n"
        f"4. Each lane's queue:\n{queues}\n"
        "5. `fa orch inbox --json`\n"
        "6. `fa orch questions --json`\n"
        "7. `fa orch log`\n"
        "Then re-arm the watches the note lists and keep its open promises."
    )


def refresh(repo_root: Path, store: StateStore, config: Config, now: datetime) -> str:
    """Open a fresh orchestrator; on success the old session is retired. Returns the line to print.

    The openers' lock covers the checks, the launch and the activation, so a
    second refresh waits, then finds the note consumed; a gate spawn cannot
    activate its session between this launch and this activation.
    """
    with watch_open._opening(store):
        return _refresh_locked(repo_root, store, config, now)


def _refresh_locked(repo_root: Path, store: StateStore, config: Config, now: datetime) -> str:
    language = config.language
    note = _fresh_handoff(store, language)
    _quiet(store, now, language)
    old = orch_sessions.active(store)
    old_id = old["session_id"] if old else None
    session = orch_spawn.spawn_orchestrator(repo_root, store, config, prompt=first_prompt(config, note, old_id),
                                            now=now)
    if not session:
        append_event(store, now, None, "refresh-failed", "fresh orchestrator session did not open",
                     previous_session_id=old_id)
        raise RefreshRefused(text("orch_refresh_failed", language))
    with store._state_lock():
        orch_sessions.activate(store, session, now, locked=True)
        append_event(store, now, None, "refresh", f"session {old_id} -> {session['session_id']}", locked=True,
                     session_id=session["session_id"], previous_session_id=old_id, handoff=str(note))
    return text("orch_refresh_done", language, new=session["session_id"], old=old_id or "?")


def add_parsers(orch_actions) -> None:
    """`fa orch refresh-due`, `fa orch handoff` and `fa orch refresh` under `fa orch`.

    Help follows the repository's language like `fa report`: the current folder's
    at build time, then the one named by --repo-root just before parsing.
    """
    from foragent.commands.report import _help_language, _repo_root_in

    keyed = []  # (object with .help, message key)

    def subcommand(name, key):
        parser = orch_actions.add_parser(name, help="")
        keyed.append((orch_actions._choices_actions[-1], key))
        return parser

    due_parser = subcommand("refresh-due", "orch_refresh_due_help")
    due_parser.add_argument("--json", action="store_true")
    handoff = subcommand("handoff", "orch_handoff_help")
    handoff.add_argument("--file", type=Path, required=True)
    refresh_parser = subcommand("refresh", "orch_refresh_help")
    keyed.append((refresh_parser.add_argument("--ready", action="store_true"), "orch_refresh_ready_help"))

    def localize(args) -> None:
        root = _repo_root_in(args)
        language = _help_language(Path(root) if root else Path.cwd())
        for target, key in keyed:
            target.help = text(key, language)

    localize(None)
    for parser in (due_parser, handoff, refresh_parser):
        parser.add_argument("--repo-root", type=Path, default=Path.cwd())
        parse = parser.parse_known_args

        def parse_known_args(args=None, namespace=None, parse=parse):
            localize(args)
            return parse(args, namespace)

        parser.parse_known_args = parse_known_args


def command(args, store: StateStore, config: Config, now: datetime) -> None:
    if args.action == "refresh-due":
        report = due(store, config, now)
        print(json.dumps(report, ensure_ascii=False) if args.json else report["reason"])
    elif args.action == "handoff":
        path = accept_handoff(store, config, args.file, now)
        print(text("orch_handoff_saved", config.language, path=path))
    elif not args.ready:
        raise RefreshRefused(text("orch_refresh_needs_ready", config.language))
    else:
        print(refresh(args.repo_root.resolve(), store, config, now))
