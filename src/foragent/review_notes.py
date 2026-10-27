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
