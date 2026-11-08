"""Promises: "when X happens, do Y, owner Z", recorded by any session and moved by the gate.

A session records a promise with ``fa orch promise add`` and returns at once. X is a
machine-checkable event: ``closed:<N>``, ``merged:<N>``, ``approved:<N>`` or
``time:<ISO 8601 time with a timezone>``; anything else is refused when recorded.

Writers of ``orch-promises.json`` (one lock, ``orch-promises.lock``):
- ``add`` and ``done`` (the CLI) add and remove promises;
- ``on_tick`` (the gate, once per tick) is the only writer of ``state``, ``due_at`` and
  ``late_at``. It moves waiting -> due when the event has happened (one ``promise-due``
  request to the owner) and due -> late after the limit (one ``promise-late`` request one
  level up). The owner still does Y; ``done`` removes the promise.

Readers: the /fa report and dashboard (``counts``, ``late``), the owner's next run prompt
(``prompt_section``) and the shelf place ``promises`` (``shelf_promises``).
"""

from __future__ import annotations

from datetime import datetime, timedelta
import json
from typing import Any

from foragent.state_locks import _file_lock

FILE = "orch-promises.json"
LOCK = "orch-promises.lock"
KINDS = ("closed", "merged", "approved", "time")
DEFAULT_LIMIT_MINUTES = 60
WINDOW = timedelta(hours=24)


class PromiseError(ValueError):
    """A promise that cannot be recorded or found."""


def level_up(owner: str) -> str:
    """Who hears about a late promise: the PM above the orchestrator, the orchestrator above
    lanes and roles, and the person (through the /fa report) above the PM."""
    if owner == "pm":
        return "person"
    return "pm" if owner == "orchestrator" else "orchestrator"


# ---------------------------------------------------------------- storage


def _empty() -> dict[str, Any]:
    return {"next": 1, "open": [], "moves": []}


def _read(store) -> dict[str, Any]:
    try:
        value = json.loads((store.root / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty()
    if not isinstance(value, dict) or not isinstance(value.get("open"), list):
        return _empty()
    value.setdefault("next", 1)
    value.setdefault("moves", [])
    return value


def _write(store, value: dict[str, Any], now: datetime) -> None:
    value["moves"] = [move for move in value["moves"] if _time(move.get("at")) and _time(move["at"]) > now - WINDOW]
    path = store.root / FILE
    temporary = path.with_name(f".{FILE}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")
    temporary.replace(path)


class _Locked:
    def __init__(self, store):
        self.store = store

    def __enter__(self):
        self.store.root.mkdir(parents=True, exist_ok=True)
        self._lock = _file_lock(self.store.root / LOCK)
        self._lock.__enter__()
        return _read(self.store)

    def __exit__(self, *exc):
        return self._lock.__exit__(*exc)


def _time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


def promises(store) -> list[dict[str, Any]]:
    """The open promises, oldest first."""
    return [row for row in _read(store)["open"] if isinstance(row, dict) and isinstance(row.get("id"), str)]


# ---------------------------------------------------------------- events


SUPPORTED = ("closed:<issue>, merged:<issue or foragent/ PR>, approved:<issue> "
             "or time:<ISO 8601 time with a timezone>")


def _refuse(reason: str) -> PromiseError:
    """Every event refusal names the events the gate observes, in the command's own syntax."""
    return PromiseError(f"{reason}. Supported events: {SUPPORTED}")


def parse_event(when: str) -> tuple[str, Any]:
    """(kind, target) of an event, or PromiseError naming what cannot be checked."""
    kind, sep, target = when.strip().partition(":")
    if not sep or kind not in KINDS:
        raise _refuse(f"cannot check the event {when!r}")
    if kind == "time":
        at = _time(target.strip())
        if at is None:
            raise _refuse(f"cannot check the time {target.strip()!r}: it needs an ISO 8601 time with a timezone")
        return kind, at.isoformat()
    number = target.strip().lstrip("#")
    if not number.isdigit():
        raise _refuse(f"cannot check {target!r}: it needs an issue or PR number")
    return kind, int(number)


def _observed_row(store, number: int) -> dict[str, Any] | None:
    """The issue as GitHub last showed it to the gate, or None.

    ``store.local_issues()`` overlays writes the gate queued but GitHub has not confirmed
    (a queued close shows the issue closed during a rate-limit pause), and a queued write
    can leave a placeholder row for a number never observed. A promise event is only what
    GitHub showed, so this reads the observation itself.
    """
    row = (store._issue_record().get("issues") or {}).get(str(number))
    if not row or row.get("observed_at") is None or row.get("is_pull_request"):
        return None
    return row


def _listed_pr(store, number: int) -> bool:
    """A merged foragent/ PR the gate's refresh already recorded in the GitHub issue cache."""
    rows = (store._issue_record().get("issues") or {}).values()
    return any(str(number) in (row.get("merges") or {}) for row in rows)


def check_observable(store, tracker, kind: str, number: int) -> None:
    """Refuse an event on ``number`` that the gate's tick does not observe.

    The tick observes issues through the local records it refreshes: an issue closing
    (observe_closed_issues), a run of the issue merging (merge_times or the row's own PR)
    and an approval record. Of pull requests it records only foragent/ PRs that merged
    (each issue row's ``merges``); it never records a PR closing, nor a PR on another branch.
    """
    if _observed_row(store, number) is not None:
        return
    if any(key[0] == number for key in store.merge_times()):
        return
    if kind == "merged" and _listed_pr(store, number):
        return
    try:
        issue = tracker.get_issue(number) if tracker is not None else None
    except Exception:  # noqa: BLE001 - any failed read means the gate cannot check it
        issue = None
    if issue is None:
        raise _refuse(f"cannot check #{number}: no such issue or PR is known")
    if not getattr(issue, "is_pull_request", False):
        return
    if kind == "merged":
        try:
            head = tracker.get_pr(number).head
        except Exception:  # noqa: BLE001 - an unreadable PR cannot be checked
            head = ""
        if head.startswith("foragent/"):
            return
        raise _refuse(f"cannot check merged:{number}: the gate records merges only of foragent/ PRs")
    raise _refuse(f"cannot check {kind}:{number}: #{number} is a pull request, and the gate does not "
                  f"observe a pull request being {'closed' if kind == 'closed' else 'approved'}")


def happened(store, row: dict[str, Any], now: datetime) -> bool:
    """Whether the promise's event has happened, from the records the tick refreshed."""
    kind, target = row.get("kind"), row.get("target")
    if kind == "time":
        at = _time(target)
        return at is not None and now >= at
    number = int(target)
    found = _observed_row(store, number)
    if kind == "closed":
        return found is not None and found.get("state") == "closed"
    if kind == "approved":
        return store.load_approval(number) is not None
    # merged: a gate merge of the issue's run, the issue row's own PR, or a merged foragent/ PR.
    if any(key[0] == number for key in store.merge_times()):
        return True
    return (found is not None and bool(found.get("merged"))) or _listed_pr(store, number)


# ---------------------------------------------------------------- commands


def owners(config) -> set[str]:
    """Who can be told: the PM, the orchestrator, every lane (main included) and every role."""
    from foragent.roles import load_roles

    return {"pm", "orchestrator", "main", *(config.lanes or {}), *load_roles(config)}


def add(store, tracker, when: str, action: str, owner: str, now: datetime,
        limit_minutes: int | None = None, *, known_owners: set[str] | None = None) -> dict[str, Any]:
    """Record one waiting promise and return it; refuse an event the gate cannot check
    and an owner nobody reads for (``known_owners``)."""
    if known_owners is not None and owner.strip() not in known_owners:
        raise PromiseError(f"unknown owner {owner.strip()!r}: use one of {', '.join(sorted(known_owners))}")
    kind, target = parse_event(when)
    if kind != "time":
        check_observable(store, tracker, kind, target)
    if not action.strip():
        raise PromiseError("the action (--do) must not be empty")
    if not owner.strip():
        raise PromiseError("the owner (--owner) must not be empty")
    limit = DEFAULT_LIMIT_MINUTES if limit_minutes is None else limit_minutes
    if limit <= 0:
        raise PromiseError("--limit-minutes must be positive")
    with _Locked(store) as value:
        record = {
            "id": f"p{value['next']}", "when": f"{kind}:{target}", "kind": kind, "target": target,
            "do": " ".join(action.split()), "owner": owner.strip(), "limit_minutes": limit,
            "state": "waiting", "created_at": now.isoformat(),
        }
        value["next"] += 1
        value["open"].append(record)
        value["moves"].append({"kind": "in", "at": now.isoformat()})
        _write(store, value, now)
    return record


def done(store, promise_id: str, now: datetime) -> dict[str, Any]:
    """Remove a promise whose action was done."""
    with _Locked(store) as value:
        found = next((row for row in value["open"] if row.get("id") == promise_id), None)
        if found is None:
            raise PromiseError(f"no open promise {promise_id}")
        value["open"] = [row for row in value["open"] if row is not found]
        value["moves"].append({"kind": "out", "at": now.isoformat()})
        _write(store, value, now)
    return found


# ---------------------------------------------------------------- the gate


def _sent(store, kind: str, promise_id: str) -> bool:
    from foragent import orch_inbox

    return any(row.get("kind") == kind and row.get("promise") == promise_id
               for row in orch_inbox.requests(store))


def _tell(store, kind: str, row: dict[str, Any], to: str, text: str) -> None:
    from foragent import orch_inbox

    if not _sent(store, kind, row["id"]):  # A crash after the request but before the save sends once.
        orch_inbox.add_request(store, "gate", text, {"kind": kind, "promise": row["id"], "to": to,
                                                     "owner": row["owner"], "do": row["do"]})


def _move_due(store, row: dict[str, Any], now: datetime) -> bool:
    """Move one waiting promise whose event happened to due and tell its owner once; whether it moved.

    Called with the promise lock held, by the tick and by every owner launch (``deliver``).
    """
    if row.get("state") != "waiting" or not happened(store, row, now):
        return False
    _tell(store, "promise-due", row, row["owner"],
          f"Promise {row['id']} is due ({row['when']}): {row['do']}. "
          f"Mark it done with `fa orch promise done {row['id']}`.")
    row["state"], row["due_at"] = "due", now.isoformat()
    return True


def mark_due(store, now: datetime) -> list[str]:
    """Move every waiting promise whose event already happened to due; the ids that moved."""
    if not (store.root / FILE).exists():
        return []
    with _Locked(store) as value:
        moved = [row["id"] for row in value["open"]
                 if isinstance(row, dict) and "id" in row and _move_due(store, row, now)]
        if moved:
            _write(store, value, now)
    return moved


def deliver(store, owner: str) -> str:
    """Before an owner's session is launched: mark due promises, then the owner's prompt section.

    Every owner launch path calls this (orchestrator spawn, lane runs, role sessions), so a
    promise whose event happened before the launch reaches its owner even when no tick ran
    in between. Due-ness in the section is still derived at build time, so a failed marking
    never hides a promise.
    """
    try:
        mark_due(store, store._now())
    except (OSError, ValueError):
        pass
    return prompt_section(store, owner)


def on_tick(ctx) -> list[str]:
    """Move every promise one step if its event or limit has passed; tell its reader once."""
    store, now = ctx.store, ctx.now()
    if not (store.root / FILE).exists():
        return []
    moved = []
    with _Locked(store) as value:
        for row in value["open"]:
            if not isinstance(row, dict) or "id" not in row:
                continue
            if _move_due(store, row, now):
                moved.append(f"Promise {row['id']} is due for {row['owner']}")
            due_at = _time(row.get("due_at"))
            if row.get("state") == "due" and due_at is not None and \
                    now - due_at > timedelta(minutes=row.get("limit_minutes") or DEFAULT_LIMIT_MINUTES):
                up = level_up(row["owner"])
                _tell(store, "promise-late", row, up,
                      f"Promise {row['id']} of {row['owner']} is late: {row['do']} "
                      f"(due since {due_at.isoformat()}, limit {row.get('limit_minutes')} minutes).")
                row["state"], row["late_at"] = "late", now.isoformat()
                moved.append(f"Promise {row['id']} of {row['owner']} is late")
        if moved:
            _write(store, value, now)
    return moved


# ---------------------------------------------------------------- readers


def counts(store) -> tuple[int, int]:
    """(open promises, late promises)."""
    rows = promises(store)
    return len(rows), sum(row.get("state") == "late" for row in rows)


def late(store) -> list[dict[str, Any]]:
    return [row for row in promises(store) if row.get("state") == "late"]


def shelf_row(store, now: datetime) -> dict[str, Any]:
    """The shelf place's numbers: open count, moves in and out over 24 h, oldest age."""
    value = _read(store)
    rows = [row for row in value["open"] if isinstance(row, dict)]
    recent = [move for move in value["moves"] if (_time(move.get("at")) or now - WINDOW) > now - WINDOW]
    created = [at for at in (_time(row.get("created_at")) for row in rows) if at is not None]
    return {
        "count": len(rows),
        "in_24h": sum(move.get("kind") == "in" for move in recent),
        "out_24h": sum(move.get("kind") == "out" for move in recent),
        "oldest_minutes": int((now - min(created)).total_seconds() // 60) if created else None,
        "late": sum(row.get("state") == "late" for row in rows),
    }


def prompt_section(store, owner: str) -> str:
    """The owner's due and late promises for its next run prompt, or ''.

    Due-ness is derived when the prompt is built, not read from the tick's stored state: a run
    the same tick starts (or resumes) before its promise step still lists a promise whose event
    has already happened. The stored state stays the tick's to move (notices, late alarms).
    """
    now = store._now()
    rows = []
    for row in promises(store):
        if row.get("owner") != owner:
            continue
        shown = row.get("state")
        if shown == "waiting" and happened(store, row, now):
            shown = "due"
        if shown in ("due", "late"):
            rows.append({**row, "state": shown})
    if not rows:
        return ""
    lines = [f"- {row['id']} ({row['state']}, {row['when']}): {row['do']}" for row in rows]
    return ("\n\nYour promises that are due (do the action, then run `fa orch promise done <id>`):\n"
            + "\n".join(lines) + "\n")
