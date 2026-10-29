"""`fa brief --report`: one judgment-first message since the previous report (#756).

Reads local state only: gate merge records, the orchestrator journal
(orch-events.jsonl), the question box, the orchestrator note and the issue
snapshot (for titles and open needs-human issues). The report command is the only writer of the report time
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
from foragent.labels import NEEDS_HUMAN
from foragent.update_check import offer_line
from foragent.state import StateStore

REPORT_FILE = "report.json"
FIRST_WINDOW = timedelta(hours=24)
# Holds and their ends come from the append-only orch-events.jsonl only (overwritten
# snapshots lose earlier holds). A merge from the gate's merge records also ends a hold.
HOLD_KINDS = frozenset({"escalate", "retries-exceeded", "stalled", "orchestrator-timeout", "parked"})
RESOLVING_KINDS = frozenset({"retry", "resolve", "unparked"})


def last_report(store: StateStore, now: datetime) -> datetime | None:
    """The previous report time; a missing, broken or future time counts as none."""
    try:
        value = json.loads((store.root / REPORT_FILE).read_text(encoding="utf-8"))["reported_at"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    at = _time(value)
    return at if at is not None and at <= now else None


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
    """Each issue's latest gate merge (record_merge's merged_at), the source the statusline counts."""
    merges: dict[int, datetime] = {}
    for (number, started), value in presence.gate_merges(store).items():
        at, start = _time(value), _time(started)
        if at is not None and start is not None and at >= start:
            merges[number] = max(at, merges.get(number, at))
    return merges


def _resolved_holds(events: list[dict[str, Any]], merges: dict[int, datetime],
                    inside) -> dict[int, tuple[str, str | None]]:
    """Issues with a hold event (of any age) and a later resolving event inside the window.

    The value is the latest resolving event's kind ("merge" for a merge) and its say line,
    None when it has none (the report then uses a default sentence). Earlier holds are kept: a later hold does not hide them.
    """
    first_hold: dict[int, datetime] = {}
    for event in events:
        if event.get("kind") in HOLD_KINDS and isinstance(number := event.get("issue"), int):
            first_hold[number] = min(event["at"], first_hold.get(number, event["at"]))
    resolved = {}
    for number, held in first_hold.items():
        ends = [(event["at"], event["kind"], event.get("say")) for event in events
                if event.get("issue") == number and event.get("kind") in RESOLVING_KINDS]
        if number in merges:
            ends.append((merges[number], "merge", None))
        ends = [end for end in ends if end[0] > held and inside(end[0])]
        if ends:
            resolved[number] = max(ends, key=lambda end: end[0])[1:]
    return resolved


def collect(store: StateStore, now: datetime) -> dict[str, Any]:
    since = last_report(store, now) or now - FIRST_WINDOW

    def inside(at: datetime) -> bool:
        return since < at <= now

    merges = _merges(store)
    titles = {row["number"]: row.get("title") for row in store.local_issues()}
    events = _events(store)
    holds = _resolved_holds(events, merges, inside)
    resolved = [{"issue": number, "by": by, "say": say} for number, (by, say) in sorted(holds.items())]
    # A merged issue whose hold was resolved appears once, under holds resolved.
    merged = [{"issue": number, "title": titles.get(number)}
              for number, at in sorted(merges.items()) if inside(at) and number not in holds]
    notes = [event for event in events if event.get("kind") == "note"
             and isinstance(event.get("say"), str) and event["say"].strip()]
    note = orch_inbox.read_note(store) or {}
    runs = store.active_runs()
    person = sorted(row["number"] for row in store.local_issues()
                    if row.get("state") == "open" and NEEDS_HUMAN in (row.get("labels") or ()))
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
                       line=item["say"] or text("brief_report_resolved_merged" if item["by"] == "merge"
                                                 else "brief_report_resolved_default", language))
                  for item in report["resolved"]]
    else:
        lines.append(text("brief_report_nothing_finished", language))
    now = text("brief_report_now", language, working=report["working"], lanes=report["lanes"])
    lines.append(f"{now} · {report['now']}" if report["now"] else now)
    if not turns:
        lines.append(text("brief_report_nothing_needed", language))
        return "\n".join(lines + _update(report, language))
    lines.append(text("brief_report_your_turn", language))
    recommended = text("brief_report_recommended", language)
    for question in report["questions"]:
        choices = " / ".join(
            f"{index}. {option}" + (f" ({recommended})" if index == question.get("recommend") else "")
            for index, option in enumerate(question.get("options") or [], 1))
        lines.append(f"- {question['question']}" + (f" {choices}" if choices else ""))
    lines += [text("brief_report_person_issue", language, issue=issue(item)) for item in report["person_issues"]]
    return "\n".join(lines + _update(report, language))


def _update(report: dict[str, Any], language: str) -> list[str]:
    """The cached update offer as the report's last line, when there is one."""
    offer = report.get("update")
    return [offer_line(offer, language)] if offer else []


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
