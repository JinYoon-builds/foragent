"""Reviewer NOTE lines: root-cause and structure findings that are recorded, never a rejection.

A NOTE line is ``NOTE: <text>`` (optionally after a list marker). It stays in the raw review,
the PR body, review comments and the run log, but never in the findings an implementer
receives. Each distinct NOTE (whitespace and case normalised) is journaled once per issue as
a ``review-note`` event.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
import json
import re

from foragent import orch_events
from foragent.state import StateStore

_NOTE = re.compile(r"^\s*(?:[-*]\s+|\d+[.)]\s+)?`?NOTE:`?\s*(.*?)\s*$")
KIND = "review-note"


def note_text(line: str) -> str | None:
    """The note of one NOTE line, else None."""
    match = _NOTE.match(line)
    return match[1] if match and match[1] else None


def strip(text: str) -> str:
    """The text without its NOTE lines; a text without any is returned unchanged."""
    lines = text.splitlines(keepends=True)
    if all(note_text(line) is None for line in lines):
        return text
    return "".join(line for line in lines if note_text(line) is None).rstrip("\n")


def normalize(note: str) -> str:
    return " ".join(note.split()).casefold()


def unique(notes: Iterable[str]) -> tuple[str, ...]:
    kept: dict[str, str] = {}
    for note in notes:
        kept.setdefault(normalize(note), " ".join(note.split()))
    return tuple(kept.values())


def extract(*texts: str) -> tuple[str, ...]:
    """Every distinct note, in order, across the given texts."""
    return unique(note for text in texts for line in text.splitlines()
                  if (note := note_text(line)) is not None)


def record(store: StateStore, at: datetime, issue: int, notes: Iterable[str], **fields) -> list[str]:
    """Journal each note not yet recorded for this issue; return the newly recorded ones."""
    notes = unique(notes)
    if not notes:
        return []
    added = []
    with store._state_lock():
        path = store.root / "orch-events.jsonl"
        seen = set()
        if path.is_file():
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get("kind") == KIND and event.get("issue") == issue:
                    seen.add(normalize(str(event.get("note", event.get("summary", "")))))
        for note in notes:
            if normalize(note) in seen:
                continue
            seen.add(normalize(note))
            orch_events.append_event(store, at, issue, KIND, note, locked=True, note=note, **fields)
            added.append(note)
    return added


ARCHITECT_LIMIT = 20


def _events(store: StateStore) -> list[dict]:
    path = store.root / "orch-events.jsonl"
    if not path.is_file():
        return []
    events = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _when(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def for_architect(store: StateStore, role: str = "architect") -> list[dict]:
    """Recorded notes the next ``role`` session has not yet seen, oldest first.

    Derived from the journal alone: the notes recorded since the start of the last ``<role>-done``
    session that exited 0, or every note when no session finished cleanly. Nothing is consumed or
    deleted; a failed session leaves the same notes for the next one.
    """
    events = _events(store)
    since = None
    for event in events:
        if event.get("kind") == f"{role}-done" and event.get("exit_code") == 0:
            started = _when(event.get("started_at"))
            if started is not None and (since is None or started > since):
                since = started
    notes = []
    for event in events:
        if event.get("kind") != KIND:
            continue
        at = _when(event.get("at"))
        if since is not None and (at is None or at < since):
            continue
        notes.append({"issue": event.get("issue"), "round": event.get("round"), "at": event.get("at"),
                      "note": str(event.get("note", event.get("summary", "")))})
    return notes


def architect_section(store: StateStore, role: str = "architect") -> str:
    """The prompt section listing the recorded review notes, or "" when there are none."""
    notes = for_architect(store, role)
    if not notes:
        return ""
    shown = notes[::-1][:ARCHITECT_LIMIT]
    lines = ["## Recorded structural proposals from reviews", "",
             "Reviewers recorded these NOTE lines (root causes and structure outside each issue's Goal).",
             "They never blocked a merge and never started lane rework. Weigh them as evidence; deliver",
             "any you adopt as your own proposal through `fa orch request`.", ""]
    for note in shown:
        round_part = f"round {note['round']}, " if note["round"] is not None else ""
        lines.append(f"- #{note['issue']} ({round_part}{note['at']}): {note['note']}")
    older = len(notes) - len(shown)
    journal = store.root / "orch-events.jsonl"
    lines += ["", (f"{older} older notes are not shown. " if older else "")
              + f"Full source: the `{KIND}` events in {journal}."]
    return "\n".join(lines)
