"""Finite local flow records: what happened and when, kept apart from every decision.

The records live under ``<state>/flow/`` and stay local. Ticks and all other
kinds are separate groups, so frequent ticks never push decision records out.
Each group keeps two generations; a full generation replaces the older one, and
no record is ever rewritten. Limits include the issue-fact index, so every
generation together stays within 2,048 records and 8 MiB. Missing, malformed or
evicted evidence is simply absent from the result: it is unknown, never a zero.

Recording is observation: a failure here never replaces a result or an error.
Lock order is state lock, then flow lock; nothing takes the state lock under the flow lock.
"""

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Any

from foragent.state_locks import _file_lock
from foragent.state_observations import accepted_facts, stamp

MiB = 1024 * 1024
# Group: (records per generation, bytes per generation). Two generations each.
GENERATIONS = {"tick": (256, MiB), "other": (768, 2 * MiB + 3 * MiB // 4)}
INDEX_BYTES = MiB // 2
RECORD_BYTES = 64 * 1024
Change = tuple[int | None, str | None, dict[str, Any]]


def _group(kind: str) -> str:
    return "tick" if kind == "tick" else "other"


def normalized_edges(edges: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each known blocked_by edge's state and repository, in one stable order."""
    known = [{"number": edge["number"], "state": edge.get("state"), "repository": edge.get("repository") or None}
             for edge in edges]
    return sorted(known, key=lambda edge: (edge["repository"] or "", edge["number"]))


def edge_read(edges: Iterable[dict[str, Any]], started_at: datetime) -> dict[str, Any]:
    """One observation of an issue's whole edge list, at the time its read started."""
    return {"at": started_at.isoformat(), "edges": normalized_edges(edges)}


def _with_fact(kept: list[dict[str, Any]], fact: dict[str, Any]) -> list[dict[str, Any]]:
    """Add a fact in time order, whenever it arrives.

    Before the newest whole list, only each edge's latest edit can still matter (see ``current_edges``).
    """
    facts = sorted([*kept, fact], key=lambda item: stamp(item["at"]))
    whole = [position for position, item in enumerate(facts) if "edges" in item]
    if not whole:
        return facts
    latest = {item["edge"]["number"]: item for item in facts[:whole[-1]] if "edge" in item}
    return [*sorted(latest.values(), key=lambda item: stamp(item["at"])), *facts[whole[-1]:]]


def current_edges(kept: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """The edges the facts add up to, in time order. Removing from unknown edges leaves them unknown.

    A write happened somewhere between its start (``at``) and its response (``accepted_at``).
    So a whole list decides an edge unless the edge's latest earlier write was still in flight
    when the list's read started: that read may not show it, and the write stands.
    """
    known = None
    for position, fact in enumerate(kept):
        if "edges" not in fact:
            known = _edit(known, fact)
            continue
        known = fact["edges"]
        for edit in kept[:position]:
            if stamp(edit["accepted_at"]) > stamp(fact["at"]):
                known = _edit(known, edit)
    return known


def _edit(known: list[dict[str, Any]] | None, fact: dict[str, Any]) -> list[dict[str, Any]] | None:
    edge = fact["edge"]
    rest = [item for item in known or [] if item["number"] != edge["number"] or item["repository"]]
    return (rest if known is not None else None) if fact["remove"] else normalized_edges([*rest, edge])


def _valid(row: Any) -> bool:
    if not isinstance(row, dict) or not isinstance(row.get("kind"), str) or not isinstance(row.get("data"), dict):
        return False
    return {"issue", "run_started_at"} <= row.keys() and stamp(row.get("at")) is not None


def _rows(path: Path) -> list[dict[str, Any]]:
    try:
        raw = path.read_bytes()
    except OSError:
        return []
    rows = []
    for line in raw.split(b"\n"):
        try:
            row = json.loads(line)
        except ValueError:
            continue  # A torn or malformed line is unknown, not a reason to lose the rest.
        if _valid(row):
            rows.append(row)
    return rows


def _encoded(row: dict[str, Any]) -> bytes:
    line = json.dumps(row, ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(line) <= RECORD_BYTES:
        return line
    # The newest record stays readable within the limit; its size says what was dropped.
    short = {**row, "data": {"truncated": True, "bytes": len(line)}}
    return json.dumps(short, ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8") + b"\n"


class FlowRecordsMixin:
    def _flow_dir(self) -> Path:
        return self.root / "flow"

    def _flow_path(self, group: str, older: bool = False) -> Path:
        return self._flow_dir() / f"{group}{'.1' if older else ''}.jsonl"

    def _flow_group_rows(self, group: str) -> list[dict[str, Any]]:
        return [*_rows(self._flow_path(group, older=True)), *_rows(self._flow_path(group))]

    def _flow_append(self, group: str, lines: list[bytes]) -> None:
        """Append under the flow lock; a full generation replaces the older one. Never rewrites a line."""
        if not lines:
            return
        path, (limit, size_limit) = self._flow_path(group), GENERATIONS[group]
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            raw = b""
        count, size = raw.count(b"\n"), len(raw)
        pending, torn = [], bool(raw) and not raw.endswith(b"\n")  # An interrupted append has no newline.

        def flush() -> None:
            nonlocal pending, torn
            if pending:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
                try:
                    os.write(descriptor, (b"\n" if torn else b"") + b"".join(pending))
                finally:
                    os.close(descriptor)
                pending, torn = [], False

        for line in lines:
            if count >= limit or size + len(line) + 1 > size_limit:
                flush()
                if path.exists():
                    os.replace(path, self._flow_path(group, older=True))
                count, size, torn = 0, 0, False
            pending.append(line)
            count, size = count + 1, size + len(line)
        flush()

    def _flow_row(self, kind: str, issue: int | None, run_started_at: str | None, data: dict[str, Any]) -> dict[str, Any]:
        return {"at": self._timestamp(), "kind": kind, "issue": issue, "run_started_at": run_started_at, "data": data}

    def record_flow(self, kind: str, *, issue: int | None = None, run_started_at: str | None = None,
                    **data: Any) -> None:
        """Record one observation at the store's UTC clock, whatever its data repeats."""
        try:
            line = _encoded(self._flow_row(kind, issue, run_started_at, data))
            with _file_lock(self._flow_dir() / "flow.lock"):
                self._flow_append(_group(kind), [line])
        except Exception:  # noqa: BLE001
            return  # Observation only: the caller's own result and error stay its own.

    def flow_records(self, *, since: datetime | None = None, until: datetime | None = None) -> list[dict[str, Any]]:
        """Retained observations in ``[since, until)``, oldest first. Reading changes nothing."""
        with self._flow_reading():
            rows = [row for group in GENERATIONS for row in self._flow_group_rows(group)]
        moments = [(stamp(row["at"]), index) for index, row in enumerate(rows)]
        return [rows[index] for at, index in sorted(moments)
                if (since is None or at >= since) and (until is None or at < until)]

    @contextmanager
    def _flow_reading(self) -> Iterator[None]:
        """Keep a rotation from hiding a generation, without creating anything."""
        try:
            stream = (self._flow_dir() / "flow.lock").open(encoding="utf-8")
        except OSError:
            yield  # Without a lock file nobody has written a record yet.
            return
        with stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
            except OSError:
                pass
            yield

    def _record_flow_changes(self, kind: str, changes: list[Change]) -> None:
        """Add each change whose data differs from the issue's last retained record of this kind.

        The comparison and the append share the flow lock, so concurrent or
        replayed observations of one transition add one record. A new run of the
        issue is a change even when its data repeats.
        """
        if not changes:
            return
        try:
            with _file_lock(self._flow_dir() / "flow.lock"):
                last = {row["issue"]: row for row in self._flow_group_rows(_group(kind)) if row["kind"] == kind}
                self._flow_append(_group(kind), [
                    _encoded(self._flow_row(kind, issue, started, data)) for issue, started, data in changes
                    if issue not in last or (last[issue]["data"], last[issue]["run_started_at"]) != (data, started)])
        except Exception:  # noqa: BLE001
            return

    def _flow_last(self, kind: str) -> dict[int | None, dict[str, Any]]:
        """The last retained record per issue of one kind."""
        with self._flow_reading():
            rows = self._flow_group_rows(_group(kind))
        return {row["issue"]: row for row in rows if row["kind"] == kind}

    # Issue facts: the index remembers what was last recorded per issue, so an evicted
    # record is not written again by every later listing of an unchanged issue.

    def _flow_index_path(self) -> Path:
        return self._flow_dir() / "issue-facts.json"

    def _flow_index(self) -> dict[str, Any]:
        try:
            index = json.loads(self._flow_index_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return index if isinstance(index, dict) else {}

    def flow_known_edges(self, number: int) -> list[dict[str, Any]] | None:
        """The issue's blocked_by edges as its kept facts give them; None when nothing ever established them."""
        entry = self._flow_index().get(str(number))
        return current_edges(entry.get("edge_facts", [])) if isinstance(entry, dict) else None

    def _flow_write_index(self, index: dict[str, Any]) -> None:
        text = json.dumps(index, ensure_ascii=False, separators=(",", ":"))
        while len(text.encode("utf-8")) > INDEX_BYTES:
            # Forgetting the oldest issues costs at most one repeated record each.
            for key in sorted(index, key=int)[:max(1, len(index) // 10)]:
                del index[key]
            text = json.dumps(index, ensure_ascii=False, separators=(",", ":"))
        self._atomic_write(self._flow_index_path(), text)

    def record_edges_read(self, number: int, edges: list[dict[str, Any]], started_at: datetime) -> None:
        """A read that started at ``started_at`` showed these blocked_by edges."""
        self._flow_edge_fact(number, lambda snapshot: edge_read(edges, started_at))

    def record_edge_write(self, number: int, blocker: int, started_at: datetime, accepted_at: datetime,
                          *, remove: bool) -> None:
        """A blocked_by write GitHub accepted. Recording it reads nothing, so the new edge's state is the local one."""
        self._flow_edge_fact(number, lambda snapshot: {
            "at": started_at.isoformat(), "accepted_at": accepted_at.isoformat(), "remove": remove,
            "edge": {"number": blocker, "state": (accepted_facts(snapshot, blocker)[0] or {}).get("state")}})

    def _flow_edge_fact(self, number: int, fact: Any) -> None:
        try:
            with self._state_lock():
                snapshot = self._issue_snapshot()
                self._flow_issue_states(snapshot, [number], {number: fact(snapshot)})
        except Exception:  # noqa: BLE001
            return

    def _flow_issue_states(self, snapshot: dict[str, Any], numbers: Iterable[int],
                           edges: dict[int, dict[str, Any]] | None = None) -> None:
        """Record the accepted facts of these issues where they changed; ``snapshot`` is already committed.

        ``edges`` holds the blocked_by fact one read or write established per issue.
        Only these establish edges, never a row's field. Other issues keep their edges,
        and None means they were never known.
        """
        if not numbers:
            return
        try:
            with _file_lock(self._flow_dir() / "flow.lock"):
                index, lines, dirty = self._flow_index(), [], False
                for number in sorted(numbers):
                    entry = index.get(str(number))
                    entry = entry if isinstance(entry, dict) else {}
                    facts, seen = accepted_facts(snapshot, number)
                    if seen is None:
                        continue  # A local placeholder row: nothing about the issue was observed yet.
                    kept = entry.get("edge_facts", [])
                    if facts is not None and number in (edges or {}):
                        kept = _with_fact(kept, edges[number])
                    state = "absent" if facts is None else facts["state"]
                    data = {"labels": sorted(facts["labels"]) if facts else [],
                            "blocked_by": current_edges(kept), "state": state}
                    digest = sha256(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()[:16]
                    fresh = {"digest": digest, "edge_facts": kept}
                    if fresh != entry:
                        index[str(number)], dirty = fresh, True
                    if entry.get("digest") != digest:
                        lines.append(_encoded(self._flow_row("issue-state", number, None, data)))
                self._flow_append("other", lines)  # The index follows: a crash repeats, never loses, a record.
                if dirty:
                    self._flow_write_index(index)
        except Exception:  # noqa: BLE001
            return
