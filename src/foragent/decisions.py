"""Decision links on issues and the derived linkage/completion view (`fa decisions`).

An issue's explicit marker is the only intent record. Older issues without a
marker fall back to exact D-number citations in their published order spec; that
proves a link, never that code fulfils the decision. State is derived from the
last recorded issue observations and is never stored.
"""

import json
import re
from pathlib import Path
from typing import Any

ID = re.compile(r"D[1-9][0-9]*")
MARKER = re.compile(r"^<!-- foragent:decisions (\[[^\r\n]*\]) -->\r?$", re.MULTILINE)
MARKER_LINE = re.compile(MARKER.pattern + r"\n?", re.MULTILINE)
HEADING = re.compile(r"^### (D[1-9][0-9]*)\.", re.MULTILINE)
# An exact citation: D74 or the subclause D74-2, but never D740, D74x, D74_1 or XD74.
CITATION = re.compile(r"(?<![A-Za-z0-9_])(D[1-9][0-9]*)(?![A-Za-z0-9_])")
CHILD = re.compile(r"<!-- foragent:plan-child id=([A-Za-z0-9][A-Za-z0-9_-]*) key=")
# The order record names created children in map and every published child or edit in acceptance.
PARENT = re.compile(r"<!-- foragent:plan id=([A-Za-z0-9][A-Za-z0-9_-]*) map=(\{[^\n]*?\})"
                    r"(?: acceptance=(\{[^\n]*\}))? -->")
SPEC_LINK = re.compile(r"(?<![A-Za-z0-9_.-])docs/specs/([A-Za-z0-9][A-Za-z0-9_-]*)\.md(?![A-Za-z0-9_])")
LEDGER = "docs/decisions.md"

NOTHING_ORDERED, IN_PROGRESS, DONE = "decided, nothing ordered", "in progress", "done"


def validate(value: Any, label: str) -> list[str]:
    """A list of ledger heading IDs (D79); a subclause such as D79-1 is not an ID."""
    if not isinstance(value, list) or not all(isinstance(item, str) and ID.fullmatch(item) for item in value):
        raise ValueError(f"{label} must be a list of decision IDs such as D79")
    return value


def canonical(ids: list[str]) -> list[str]:
    return sorted(set(ids), key=lambda identity: int(identity[1:]))


def marker(ids: list[str]) -> str:
    return f"<!-- foragent:decisions {json.dumps(canonical(ids))} -->"


def explicit(body: str) -> list[str] | None:
    """The issue's explicit links, [] when explicitly unlinked, None without a marker."""
    found = MARKER.findall(body or "")
    if not found:
        return None
    try:
        ids = json.loads(found[-1])
        return canonical(validate(ids, "decision marker"))
    except ValueError:
        return None


def with_marker(body: str, ids: list[str] | None, previous: str | None) -> str:
    """The body with exactly one canonical marker: new links, else the previous issue's marker kept.

    Marker lines already in the generated text (for example quoted in a goal) are removed
    first, so they can neither duplicate nor override the links."""
    body = MARKER_LINE.sub("", body)
    if ids is None:
        kept = explicit(previous or "")
        if kept is None:
            return body
        ids = kept
    return body.rstrip("\n") + "\n" + marker(ids) + "\n"


def ledger_ids(text: str) -> list[str]:
    return list(dict.fromkeys(HEADING.findall(text)))


def _citations(text: str) -> set[str]:
    return set(CITATION.findall(text))


def _spec_names(row: dict[str, Any], parents: dict[int, set[str]]) -> set[str]:
    # The publisher writes its own marker after the text, so an earlier one is quoted text.
    body = row.get("body") or ""
    references = set(SPEC_LINK.findall(body)) if PARENT.search(body) else set()
    return set(CHILD.findall(body)[-1:]) | references | parents.get(row["number"], set())


def _parent_map(rows: list[dict[str, Any]]) -> dict[int, set[str]]:
    """Children named in each published order record's issue map, by spec name."""
    found: dict[int, set[str]] = {}
    for row in rows:
        for identity, mapping, acceptance in PARENT.findall(row.get("body") or "")[-1:]:
            numbers: list[Any] = []
            for text, read in ((mapping, lambda value: value.values()),
                               (acceptance, lambda value: [int(key) for key in value if key.isdecimal()])):
                try:
                    numbers += read(json.loads(text)) if text else []
                except (ValueError, AttributeError, TypeError):
                    continue
            for number in numbers:
                if type(number) is int and number > 0:
                    found.setdefault(number, set()).add(identity)
    return found


def _finished(row: dict[str, Any] | None) -> bool:
    """Observed completion: closed as completed, or closed with an observed merge and no other reason."""
    if row is None or row.get("state") != "closed":
        return False
    reason = row.get("state_reason")
    return reason == "completed" or (reason is None and row.get("merged") is True)


def observed_rows(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Read observation-ingest's facts and absence view; write intents are not observations."""
    from foragent.state_observations import latest

    rows = []
    for row in sorted(snapshot["issues"].values(), key=lambda row: row["number"]):
        facts, seen = latest(snapshot, row["number"])
        if facts is None:
            # Keep known links, but an absent or unobserved row cannot prove completion.
            facts = {"state": "unknown", "state_reason": None, "merged": False}
        rows.append({**row, **facts, "observed_at": seen.isoformat() if seen is not None else None})
    return rows


def view(root: Path, rows: list[dict[str, Any]], ledger: str) -> list[dict[str, Any]]:
    """One row per ledger heading from local issue facts and published specs; reads only."""
    ids = ledger_ids(ledger)
    links: dict[str, set[int]] = {identity: set() for identity in ids}
    parents = _parent_map(rows)
    specs: dict[str, set[str]] = {}
    observed = {row["number"] for row in rows}
    # A published map names its children even when the snapshot has no row for one:
    # the link stays, and the missing observation keeps the decision short of done.
    unobserved = [{"number": number, "body": ""} for number in sorted(set(parents) - observed)]
    for row in [*rows, *unobserved]:
        linked = explicit(row.get("body") or "")
        if linked is None:
            linked = set()
            for name in _spec_names(row, parents):
                if name not in specs:
                    try:
                        specs[name] = _citations((root / "docs/specs" / f"{name}.md").read_text(encoding="utf-8"))
                    except (FileNotFoundError, UnicodeDecodeError, IsADirectoryError):
                        specs[name] = set()
                linked |= specs[name]
        for identity in linked:
            if identity in links:
                links[identity].add(row["number"])
    by_number = {row["number"]: row for row in rows}
    result = []
    for identity in ids:
        numbers = sorted(links[identity])
        linked_rows = [by_number.get(number) for number in numbers]
        if not numbers:
            state = NOTHING_ORDERED
        else:
            state = DONE if all(_finished(row) for row in linked_rows) else IN_PROGRESS
        times = [row.get("observed_at") if row else None for row in linked_rows]
        oldest = min(times) if times and all(times) else None
        result.append({"id": identity, "issues": numbers, "state": state, "observed_at": oldest})
    return result
