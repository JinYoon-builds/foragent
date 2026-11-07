"""Decision links on issues and the derived linkage/completion view (`fa decisions`).

An issue's explicit marker is the only intent record. Older issues without a
marker fall back to exact D-number citations in their order's spec: the spec text
in the order's parent issue, or for orders published before the parent carried it,
the citations kept in docs/decision-links.json (or a project's leftover spec file).
That proves a link, never that code fulfils the decision. Completion is derived from
the last recorded issue observations. Work left in a remaining-work record stays open
until a linked issue takes it over: any one not proven closed and untouched on GitHub when
the record was written (its first commit, else the ledger's mtime). Drops append a reason to the shared ledger.
"""

from collections.abc import Callable
from datetime import UTC, date, datetime
import json
from pathlib import Path
import re
from typing import Any

from foragent import order_parent

ID = re.compile(r"D[1-9][0-9]*")
MARKER = re.compile(r"^<!-- foragent:decisions (\[[^\r\n]*\]) -->\r?$", re.MULTILINE)
MARKER_LINE = re.compile(MARKER.pattern + r"\n?", re.MULTILINE)
HEADING = re.compile(r"^### (D[1-9][0-9]*)\.", re.MULTILINE)
# An exact citation: D74 or the subclause D74-2, but never D740, D74x, D74_1 or XD74.
CITATION = re.compile(r"(?<![A-Za-z0-9_])(D[1-9][0-9]*)(?![A-Za-z0-9_])")
CHILD = re.compile(r"<!-- foragent:plan-child id=([A-Za-z0-9][A-Za-z0-9_-]*) key=")
# The order record names created children in map and every published child or edit in acceptance.
PARENT = order_parent.PLAN
SPEC_LINK = re.compile(r"(?<![A-Za-z0-9_.-])docs/specs/([A-Za-z0-9][A-Za-z0-9_-]*)\.md(?![A-Za-z0-9_])")
LEDGER = "docs/decisions.md"
# Citations extracted once from the retired docs/specs files: order id -> decision IDs.
LEGACY_LINKS = "docs/decision-links.json"
DROP = re.compile(r"^<!-- foragent:decision-drop (\{[^\r\n]*\}) -->\r?$", re.MULTILINE)
# A rule for people or agents: nothing to build, so it never waits for an order.
RULE = re.compile(r"^<!-- foragent:decision-rule (\{[^\r\n]*\}) -->\r?$", re.MULTILINE)
# An issue that closed with work left: {"id": "D60", "issue": 1635, "what": "..."}.
REMAINING = re.compile(r"^<!-- foragent:decision-remaining (\{[^\r\n]*\}) -->\r?$", re.MULTILINE)

NOTHING_ORDERED, IN_PROGRESS, DONE = "decided, nothing ordered", "in progress", "done"
RULE_STATE = "rule, nothing to build"


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


def unordered_context(root: Path, rows: list[dict[str, Any]], ledger: str, language: str) -> str | None:
    """Unlinked ledger entries in append order, using the same facts as `fa decisions`."""
    from foragent.i18n import text

    unlinked = {row["id"] for row in view(root, rows, ledger) if row["state"] == NOTHING_ORDERED}
    headings = list(HEADING.finditer(ledger))
    entries = []
    for index, heading in enumerate(headings):
        identity = heading.group(1)
        if identity in unlinked:
            end = headings[index + 1].start() if index + 1 < len(headings) else len(ledger)
            entries.append(ledger[heading.start():end].strip())
            unlinked.remove(identity)
    return text("decisions_unordered_context", language) + "\n\n" + "\n\n".join(entries) if entries else None


def dropped(ledger: str) -> dict[str, str]:
    result = {}
    for raw in DROP.findall(ledger):
        try:
            record = json.loads(raw)
            if ID.fullmatch(record["id"]) and record["why"].strip():
                result[record["id"]] = record["why"]
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    return result


def rules(ledger: str) -> set[str]:
    found = set()
    for raw in RULE.findall(ledger):
        try:
            record = json.loads(raw)
            if ID.fullmatch(record["id"]):
                found.add(record["id"])
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    return found


def _remaining_records(ledger: str) -> dict[str, list[tuple[int, str, str]]]:
    """Each valid remaining record per decision in ledger order, with its exact ledger line."""
    found: dict[str, list[tuple[int, str, str]]] = {}
    for line in REMAINING.finditer(ledger):
        try:
            record = json.loads(line[1])
            identity, issue, what = record["id"], record["issue"], record["what"]
            if ID.fullmatch(identity) and type(issue) is int and issue > 0 and what.strip():
                found.setdefault(identity, []).append((issue, what.strip(), line[0].rstrip("\r")))
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    return found


def remaining(ledger: str) -> dict[str, list[tuple[int, str]]]:
    """Recorded work left when an issue closed, per decision, in ledger order."""
    return {identity: [(issue, what) for issue, what, _line in records]
            for identity, records in _remaining_records(ledger).items()}


def _stamp(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _closed_before(row: dict[str, Any], marked: datetime | None) -> bool:
    """Observed facts prove the issue was already closed, untouched, when the record was written.

    Its closed state was read at or before that time, or GitHub's own last update on a listing
    that showed it closed is no later. Linking an issue edits its body and closing it changes it,
    so either one after the record moves GitHub's update time past it."""
    if marked is None or row.get("state") != "closed":
        return False
    seen = _stamp(row.get("observed_at"))
    listed = row.get("observed") or {}
    updated = _stamp(listed.get("updated_at")) if listed.get("state") == "closed" else None
    return any(moment is not None and moment <= marked for moment in (seen, updated))


def _written_at(root: Path) -> Callable[[str], datetime | None]:
    """When each ledger line was written: the author date of its first commit adding that exact
    line (a rebase keeps it, unlike the commit date), else the ledger's modification time
    (uncommitted line, or no readable git history). Both are at or after the write. Reads lazily."""
    import subprocess

    added: dict[str, datetime] | None = None
    fallback: list[datetime | None] = []

    def written(line: str) -> datetime | None:
        nonlocal added
        if added is None:
            added = {}
            try:
                history = subprocess.run(
                    ["git", "-C", str(root), "log", "--reverse", "--format=foragent-date:%aI", "-p", "--", LEDGER],
                    capture_output=True, text=True, timeout=10,
                ).stdout
            except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
                history = ""
            when = None
            for text in history.splitlines():
                if text.startswith("foragent-date:"):
                    when = _stamp(text.removeprefix("foragent-date:"))
                elif when is not None and text.startswith("+") and not text.startswith("+++"):
                    added.setdefault(text[1:].rstrip("\r"), when)
        if line in added:
            return added[line]
        if not fallback:
            try:
                fallback.append(datetime.fromtimestamp((root / LEDGER).stat().st_mtime, UTC))
            except OSError:
                fallback.append(None)
        return fallback[0]

    return written


def _uncovered(records: list[tuple[int, str, str]], linked: dict[int, dict[str, Any] | None],
               written: Callable[[str], datetime | None]) -> list[str]:
    """Work left by a linked issue that no other linked issue takes over.

    Another linked issue takes over a record unless observed facts prove it was closed and
    untouched when the record was written (``_closed_before``), whatever its number: one
    still open, or linked or closed afterwards, does the remaining work. Old closed
    issues are earlier work, however many there are. An issue that itself left work
    recorded later in the append-only ledger is the next step of a chain and takes over
    this record; one whose own record came earlier is an earlier step, so a chain names
    only its last step."""
    last = {issue: position for position, (issue, _what, _line) in enumerate(records)}

    def takes_over(other: int, position: int, line: str) -> bool:
        if other in last:
            return last[other] > position
        row = linked[other]
        if row is None or row.get("state") != "closed":
            return True  # Open, reopened or not yet observed: the work is still being done there.
        return not _closed_before(row, written(line))

    return list(dict.fromkeys(
        what for position, (issue, what, line) in enumerate(records)
        if issue in linked and not any(
            other != issue and takes_over(other, position, line) for other in linked)))


def drop(root: Path, identity: str, why: str, exact: bool = False) -> None:
    """Append a reason to the shared ledger, preserving the original decision."""
    import fcntl
    import os

    if not why.strip():
        raise ValueError("decisions_drop_reason_required")
    with (root / LEDGER).open("r+", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        ledger = stream.read()
        if identity not in ledger_ids(ledger):
            raise ValueError("decisions_drop_unknown")
        record = {"id": identity, "why": why if exact else why.strip(), "date": date.today().isoformat()}
        stream.write("\n<!-- foragent:decision-drop " + json.dumps(record, ensure_ascii=False) + " -->\n")
        stream.flush()
        os.fsync(stream.fileno())


def backlog(root: Path, rows: list[dict[str, Any]], ledger: str, *, today: date | None = None) -> tuple[int, int]:
    """Count unlinked, live headings; derive their age from the heading or its first git addition."""
    import subprocess

    today = today or date.today()
    pending = {row["id"] for row in rows if row["state"] == NOTHING_ORDERED}
    born: dict[str, date] = {}
    for heading in re.finditer(r"^### (D[1-9][0-9]*)\.[^\r\n]*", ledger, re.MULTILINE):
        identity = heading[1]
        if identity not in pending:
            continue
        stamp = re.search(r"\b\d{4}-\d{2}-\d{2}\b", heading[0])
        if stamp:
            born[identity] = date.fromisoformat(stamp[0])
    undated = pending - born.keys()
    if undated:
        history = subprocess.run(
            ["git", "-C", str(root), "log", "--reverse", "--format=foragent-date:%cI", "-p", "--", LEDGER],
            capture_output=True, text=True, check=True, timeout=10,
        )
        added_on = None
        for line in history.stdout.splitlines():
            if line.startswith("foragent-date:"):
                added_on = date.fromisoformat(line.removeprefix("foragent-date:")[:10])
            elif added_on is not None and line.startswith("+### "):
                found = HEADING.match(line[1:])
                if found and found[1] in undated:
                    born.setdefault(found[1], added_on)
    return len(pending), max([0, *((today - stamp).days for stamp in born.values())])


def _citations(text: str) -> set[str]:
    return set(CITATION.findall(text))


def _spec_names(row: dict[str, Any], parents: dict[int, set[str]]) -> set[str]:
    # The publisher writes its own marker after the text, so an earlier one is quoted text.
    body = row.get("body") or ""
    references = set(SPEC_LINK.findall(order_parent.order_text(body))) if order_parent.records(body) else set()
    return set(CHILD.findall(body)[-1:]) | references | parents.get(row["number"], set())


def _legacy_links(root: Path) -> dict[str, set[str]]:
    try:
        data = json.loads((root / LEGACY_LINKS).read_text(encoding="utf-8"))
    except (FileNotFoundError, IsADirectoryError, UnicodeDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {name: {item for item in ids if isinstance(item, str) and CITATION.fullmatch(item)}
            for name, ids in data.items() if isinstance(ids, list)}


def _parent_specs(rows: list[dict[str, Any]]) -> dict[str, set[str]]:
    """Citations in each order parent's own text (summary, spec and child titles), by order id."""
    found: dict[str, set[str]] = {}
    for row in rows:
        body = row.get("body") or ""
        # Markers quoted in the order text are text; only the parent's own records count.
        for identity, _mapping, _acceptance in PARENT.findall(order_parent.records(body))[:1]:
            found.setdefault(identity, set()).update(_citations(order_parent.order_text(body)))
    return found


def _parent_map(rows: list[dict[str, Any]]) -> dict[int, set[str]]:
    """Children named in each published order record's issue map, by spec name."""
    found: dict[int, set[str]] = {}
    for row in rows:
        for identity, mapping, acceptance in PARENT.findall(order_parent.records(row.get("body") or ""))[:1]:
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
    legacy = _legacy_links(root)
    carried = _parent_specs(rows)
    observed = {row["number"] for row in rows}
    # A published map names its children even when the snapshot has no row for one:
    # the link stays, and the missing observation keeps the decision short of done.
    unobserved = [{"number": number, "body": ""} for number in sorted(set(parents) - observed)]
    for row in [*rows, *unobserved]:
        body = row.get("body") or ""
        # A parent's order text may quote a decisions marker; its intent lives in its records.
        linked = explicit(order_parent.records(body) or body)
        if linked is None:
            linked = set()
            for name in _spec_names(row, parents):
                if name not in specs:
                    try:
                        specs[name] = _citations((root / "docs/specs" / f"{name}.md").read_text(encoding="utf-8"))
                    except (FileNotFoundError, UnicodeDecodeError, IsADirectoryError):
                        specs[name] = set()
                    specs[name] |= legacy.get(name, set()) | carried.get(name, set())
                linked |= specs[name]
        for identity in linked:
            if identity in links:
                links[identity].add(row["number"])
    by_number = {row["number"]: row for row in rows}
    drops, rule_ids, records = dropped(ledger), rules(ledger), _remaining_records(ledger)
    written = _written_at(root)
    result = []
    for identity in ids:
        numbers = sorted(links[identity])
        linked_rows = [by_number.get(number) for number in numbers]
        left = _uncovered(records.get(identity, []), dict(zip(numbers, linked_rows)), written)
        if identity in drops:
            state = "dropped"
        elif not numbers:
            state = RULE_STATE if identity in rule_ids else NOTHING_ORDERED
        else:
            state = DONE if not left and all(_finished(row) for row in linked_rows) else IN_PROGRESS
        times = [row.get("observed_at") if row else None for row in linked_rows]
        oldest = min(times) if times and all(times) else None
        result.append({"id": identity, "issues": numbers, "state": state, "observed_at": oldest})
        if identity in drops:
            result[-1]["why"] = drops[identity]
        elif left:
            result[-1]["remaining"] = left
    return result
