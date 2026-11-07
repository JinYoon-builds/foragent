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


def parse_event(when: str) -> tuple[str, Any]:
    """(kind, target) of an event, or PromiseError naming what cannot be checked."""
    kind, sep, target = when.strip().partition(":")
    if not sep or kind not in KINDS:
        raise PromiseError(f"cannot check the event {when!r}: use closed:<N>, merged:<N>, approved:<N> "
                           "or time:<ISO time with a timezone>")
    if kind == "time":
        at = _time(target.strip())
        if at is None:
            raise PromiseError(f"cannot check the time {target.strip()!r}: give an ISO 8601 time with a timezone")
        return kind, at.isoformat()
    number = target.strip().lstrip("#")
    if not number.isdigit():
        raise PromiseError(f"cannot check {target!r}: give an issue or PR number")
    return kind, int(number)


def _listed_pr(store, number: int) -> bool:
    """A merged foragent/ PR the gate's refresh already recorded."""
    listing = store.merged_pr_listing() or {}
    return any(record.get("number") == number for record in listing.get("prs") or ())


def target_type(store, tracker, number: int) -> str | None:
    """"issue" or "pr" for a number the gate can observe, else None.

    Issues are observed through the local records the tick refreshes (closed, merged run,
    approval). PRs are not in those records (refresh keeps issues only, and lists merged
    PRs only for foragent/ branches), so a PR target is read on GitHub by the tick itself.
    """
    if any(row.get("number") == number for row in store.local_issues()):
        return "issue"
    if any(key[0] == number for key in store.merge_times()):
        return "issue"
    if _listed_pr(store, number):
        return "pr"
    if tracker is None:
        return None
    try:
        issue = tracker.get_issue(number)
    except Exception:  # noqa: BLE001 - any failed read means the gate cannot check it
        return None
    if issue is None:
        return None
    return "pr" if getattr(issue, "is_pull_request", False) else "issue"


def _pr_state(tracker, number: int, reads: dict) -> tuple[bool, bool] | None:
    """(closed, merged) of a PR read once per tick on GitHub; None when it cannot be read."""
    if number not in reads:
        try:
            pr = tracker.get_pr(number)
            reads[number] = (pr.state != "open" or bool(pr.merged_at), bool(pr.merged_at))
        except Exception:  # noqa: BLE001 - an unreadable PR stays waiting; the next tick reads again
            reads[number] = None
    return reads[number]


def happened(store, row: dict[str, Any], now: datetime, tracker=None, reads: dict | None = None) -> bool:
    """Whether the promise's event has happened.

    Issue events and time come from local records (the tick refreshed them). A PR event
    comes from the merged-PR listing, else from one GitHub read per tick (``reads``),
    skipped while GitHub is paused.
    """
    kind, target = row.get("kind"), row.get("target")
    if kind == "time":
        at = _time(target)
        return at is not None and now >= at
    number = int(target)
    if row.get("target_type") == "pr":
        if _listed_pr(store, number):
            return True  # A merged PR is also closed.
        if tracker is None or store.github_wait_until(now) is not None:
            return False
        state = _pr_state(tracker, number, reads if reads is not None else {})
        return state is not None and state[0 if kind == "closed" else 1]
    found = next((item for item in store.local_issues() if item.get("number") == number), None)
    if kind == "closed":
        return found is not None and found.get("state") == "closed"
    if kind == "approved":
        return store.load_approval(number) is not None
    # merged: a gate merge of the issue's run, or the issue row's own PR merged.
    if any(key[0] == number for key in store.merge_times()):
        return True
    return found is not None and bool(found.get("merged"))


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
    target_kind = None
    if kind != "time":
        target_kind = target_type(store, tracker, target)
        if target_kind is None:
            raise PromiseError(f"cannot check #{target}: no such issue or PR is known")
        if kind == "approved" and target_kind == "pr":
            raise PromiseError(f"cannot check approved:{target}: #{target} is a PR, and only issues are approved")
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
            "target_type": target_kind, "do": " ".join(action.split()), "owner": owner.strip(), "limit_minutes": limit,
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


def on_tick(ctx) -> list[str]:
    """Move every promise one step if its event or limit has passed; tell its reader once."""
    store, now = ctx.store, ctx.now()
    if not (store.root / FILE).exists():
        return []
    moved, reads = [], {}
    tracker = getattr(ctx, "tracker", None)
    with _Locked(store) as value:
        for row in value["open"]:
            if not isinstance(row, dict) or "id" not in row:
                continue
            if row.get("state") == "waiting" and happened(store, row, now, tracker, reads):
                _tell(store, "promise-due", row, row["owner"],
                      f"Promise {row['id']} is due ({row['when']}): {row['do']}. "
                      f"Mark it done with `fa orch promise done {row['id']}`.")
                row["state"], row["due_at"] = "due", now.isoformat()
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
    """The owner's due and late promises for its next run prompt, or ''."""
    rows = [row for row in promises(store) if row.get("owner") == owner and row.get("state") in ("due", "late")]
    if not rows:
        return ""
    lines = [f"- {row['id']} ({row['state']}, {row['when']}): {row['do']}" for row in rows]
    return ("\n\nYour promises that are due (do the action, then run `fa orch promise done <id>`):\n"
            + "\n".join(lines) + "\n")
