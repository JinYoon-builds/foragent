"""Foragent presence in Claude Code: the short statusline segment (#754).

Built from local state only (no GitHub call, no child process), because Claude
Code refreshes the statusline often.
"""

from datetime import datetime
from pathlib import Path

PERSON_LABELS = frozenset({"needs-human", "waiting-person"})


def project_root(start: Path) -> Path | None:
    """The nearest folder at or above ``start`` that holds .foragent.yml."""
    for folder in (start, *start.parents):
        if (folder / ".foragent.yml").is_file():
            return folder
    return None


def counts(store, now: datetime) -> tuple[int, int, int]:
    """(working lanes, person's-turn issues, merges on the local calendar day of ``now``)."""
    from foragent.orch_inbox import waiting_issues

    lanes = {run.lane for run in store.active_runs()}
    turns = {row["number"] for row in store.local_issues()
             if row.get("state") == "open" and PERSON_LABELS & set(row.get("labels") or ())}
    turns |= waiting_issues(store)
    today = now.astimezone().date()
    merged = 0
    for (_, started), value in store.merge_times().items():
        try:
            at, start = datetime.fromisoformat(value), datetime.fromisoformat(started)
        except ValueError:
            continue
        # A rerun's record carries the earlier execution's merge time; only a merge
        # after the execution started is its own (as in watch_sections.progress).
        merged += at >= start and at.astimezone().date() == today
    return len(lanes), len(turns), merged


def statusline_segment(cwd: Path, now: datetime) -> str | None:
    """The segment for a Foragent project, else None (and when the project config cannot be read)."""
    from foragent.config import load_config
    from foragent.i18n import text
    from foragent.state import StateStore

    root = project_root(cwd)
    if root is None:
        return None
    try:
        config = load_config(root)
        if not config.presence["statusline"]:
            return None
        store = StateStore.for_repo(config.repo) if config.repo else None
        # No local state yet (never run here, or no repo set): every count is zero.
        lanes, turns, merged = counts(store, now) if store and store.root.is_dir() else (0, 0, 0)
        return text("presence_statusline", config.language, lanes=lanes, turns=turns, merged=merged)
    except Exception:  # The user's statusline must keep working whatever the local state holds.
        return None


def append(output: bytes, segment: str | None) -> bytes:
    """Append ``segment`` to the last line of ``output``, keeping its final newline."""
    if not segment:
        return output
    body = output.rstrip(b"\r\n")
    separator = b"  " if body else b""
    return body + separator + segment.encode("utf-8") + output[len(body):]
