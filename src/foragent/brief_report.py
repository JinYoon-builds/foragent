"""`fa brief --report`: one judgment-first message since the previous report (#756).

Reads local state only: gate merge records, the orchestrator journal
(orch-events.jsonl), the question box, the orchestrator note and the issue
snapshot. The report command is the only writer of the report time
(``<state>/report.json``); the first report covers the last 24 hours.
Notable findings are left out until a recorded source exists.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import sys
from typing import Any

from foragent import orch_inbox, presence, watch_art
from foragent.i18n import text
from foragent.state import StateStore

REPORT_FILE = "report.json"
FIRST_WINDOW = timedelta(hours=24)
# Every recorded start of a hold (blocked or needs-human): these journal kinds, plus the
# flags/<n>.json time that actions.flag writes whenever it labels an issue blocked.
HOLD_KINDS = frozenset({"escalate", "retries-exceeded", "orchestrator-timeout", "stalled"})
# Every recorded return to ready: these journal kinds, a resolve event that requeued the
# issue, a new approval (approvals/<n>.json) and a gate merge.
READY_KINDS = frozenset({"retry", "crash-restart", "stall-restart"})


def last_report(store: StateStore) -> datetime | None:
    try:
        value = json.loads((store.root / REPORT_FILE).read_text(encoding="utf-8"))["reported_at"]
        return datetime.fromisoformat(value)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def record_report(store: StateStore, now: datetime) -> None:
    """The one writer of the report time."""
    store.root.mkdir(parents=True, exist_ok=True)
    store._write_json(store.root / REPORT_FILE, {"reported_at": now.isoformat()})


def _time(value: Any) -> datetime | None:
    try:
        at = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return at if at.tzinfo else None


def _events(store: StateStore) -> list[dict[str, Any]]:
    events = []
    try:
        with (store.root / "orch-events.jsonl").open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue  # An append still being written.
                if isinstance(event, dict) and (at := _time(event.get("at"))) is not None:
                    events.append({**event, "at": at})
    except FileNotFoundError:
        pass
    return events


def _merges(store: StateStore) -> dict[int, datetime]:
    """Each issue's latest gate merge time (an execution's own merge only)."""
    merges: dict[int, datetime] = {}
    for (number, started), value in presence.gate_merges(store).items():
        at, start = _time(value), _time(started)
        if at is not None and start is not None and at >= start:
            merges[number] = max(at, merges.get(number, at))
    return merges


def _record_time(path, key: str) -> datetime | None:
    try:
        return _time(json.loads(path.read_text(encoding="utf-8")).get(key))
    except (OSError, ValueError, AttributeError):
        return None


def _resolved_holds(store: StateStore, events: list[dict[str, Any]], merges: dict[int, datetime],
                    inside) -> dict[int, tuple[str, str | None]]:
    """Issues whose hold ended inside the window: how it ended (event, approved, merged) and the event's line."""
    holds: dict[int, datetime] = {}

    def held(number: Any, at: datetime | None) -> None:
        if isinstance(number, int) and at is not None:
            holds[number] = min(at, holds.get(number, at))

    for event in events:
        if event.get("kind") in HOLD_KINDS:
            held(event.get("issue"), event["at"])
    for path in (store.root / "flags").glob("*.json"):
        if path.stem.isdigit():
            held(int(path.stem), _record_time(path, "flagged_at"))
    returns: dict[int, list[tuple[datetime, str, str | None]]] = {}
    for event in events:
        if event.get("kind") in READY_KINDS or (event.get("kind") == "resolve" and event.get("requeued") is True):
            returns.setdefault(event.get("issue"), []).append((event["at"], "event", event.get("say") or event["summary"]))
    for number in holds:
        if (approved := _record_time(store.root / "approvals" / f"{number}.json", "approved_at")) is not None:
            returns.setdefault(number, []).append((approved, "approved", None))
        if number in merges:
            returns.setdefault(number, []).append((merges[number], "merged", None))
    resolved = {}
    for number, start in holds.items():
        ended = [item for item in returns.get(number, []) if item[0] > start and inside(item[0])]
        if ended:
            resolved[number] = max(ended, key=lambda item: item[0])[1:]
    return resolved


def collect(store: StateStore, now: datetime) -> dict[str, Any]:
    since = last_report(store) or now - FIRST_WINDOW

    def inside(at: datetime) -> bool:
        return since < at <= now

    merges = _merges(store)
    titles = {row["number"]: row.get("title") for row in store.local_issues()}
    events = _events(store)
    merged = [{"issue": number, "title": titles.get(number)}
              for number, at in sorted(merges.items()) if inside(at)]
    resolved = [{"issue": number, "via": via, "line": line}
                for number, (via, line) in sorted(_resolved_holds(store, events, merges, inside).items())]
    notes = [event for event in events if event.get("kind") == "note"
             and isinstance(event.get("say"), str) and event["say"].strip()]
    note = orch_inbox.read_note(store) or {}
    runs = store.active_runs()
    person = sorted(row["number"] for row in store.local_issues()
                    if row.get("state") == "open" and presence.PERSON_LABELS & set(row.get("labels") or ()))
    return {
        "since": since.isoformat(),
        "judgment": notes[-1]["say"] if notes else None,
        "merged": merged,
        "resolved": resolved,
        "working": len(runs),
        "lanes": len({run.lane for run in runs}),
        "now": note.get("now") if isinstance(note.get("now"), str) else None,
        "questions": orch_inbox.questions(store),
        "person_issues": [{"issue": number, "title": titles.get(number)} for number in person],
    }


def render(report: dict[str, Any], language: str = "en") -> str:
    def issue(item: dict[str, Any]) -> str:
        return f"#{item['issue']}" + (f" {item['title']}" if item.get("title") else "")

    turns = len(report["questions"]) + len(report["person_issues"])
    lines = [report["judgment"] or text("brief_report_counts", language, working=report["working"],
                                        turns=turns, merged=len(report["merged"]))]
    if report["merged"] or report["resolved"]:
        lines.append(text("brief_report_finished", language))
        lines += [text("brief_report_merged", language, issue=issue(item)) for item in report["merged"]]
        lines += [text("brief_report_resolved", language, issue=f"#{item['issue']}",
                       line=item["line"] or text(f"brief_report_resolved_{item['via']}", language))
                  for item in report["resolved"]]
    else:
        lines.append(text("brief_report_nothing_finished", language))
    now = text("brief_report_now", language, working=report["working"], lanes=report["lanes"])
    lines.append(f"{now} · {report['now']}" if report["now"] else now)
    if not turns:
        lines.append(text("brief_report_nothing_needed", language))
        return "\n".join(lines)
    lines.append(text("brief_report_your_turn", language))
    recommended = text("brief_report_recommended", language)
    for question in report["questions"]:
        choices = " / ".join(
            f"{index}. {option}" + (f" ({recommended})" if index == question.get("recommend") else "")
            for index, option in enumerate(question.get("options") or [], 1))
        lines.append(f"- {question['question']}" + (f" {choices}" if choices else ""))
    lines += [text("brief_report_person_issue", language, issue=issue(item)) for item in report["person_issues"]]
    return "\n".join(lines)


def welcome(language: str = "en", *, colour: bool | None = None) -> str:
    """The big squirrel and the greeting that leads into the setup interview."""
    colour = sys.stdout.isatty() if colour is None else colour
    rows = []
    for cells in watch_art.half_blocks(watch_art.ORCHESTRATOR, watch_art.PALETTE):
        row = ""
        for _glyph, upper, lower in cells:
            if upper is None and lower is None:
                row += " "
            elif not colour:
                row += "▀" if lower is None else "▄" if upper is None else "█"
            elif upper is None:  # Only the bottom pixel: draw it as a lower half on the terminal background.
                row += f"\x1b[38;5;{lower}m▄\x1b[0m"
            else:
                row += f"\x1b[38;5;{upper}m" + (f"\x1b[48;5;{lower}m" if lower is not None else "") + "▀\x1b[0m"
        rows.append(row.rstrip())
    return "\n".join([*rows, "", text("brief_report_greeting", language)])
