"""Rule-based places for accumulated work; readers retain all judgment.

The gate observes membership before recovery and asks every place after housekeeping.
Writers use shelf_io, which owns the filesystem operation and records membership changes.
The leaf shelf transaction never takes the state or worktree lock; alarm delivery follows it.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3
from uuid import uuid4
from weakref import ref


@dataclass(frozen=True, kw_only=True)
class Place:
    name: str
    purpose: str
    reader: str
    why: str
    trigger: str
    channel: str
    alarm: dict
    capacity: dict
    removal: str
    person_load: dict

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if value is None or value == "":
                raise ValueError(f"{field.name} is required")
        for name, keys in (("alarm", ("count", "age")), ("capacity", ("in", "out")),
                           ("person_load", ("cap", "form"))):
            value = getattr(self, name)
            if not isinstance(value, dict):
                raise TypeError(f"{name} must be a mapping")
            for key in keys:
                if key not in value or value[key] is None or value[key] == "":
                    raise ValueError(f"{name}.{key} is required")
        if type(self.alarm["count"]) is not int or self.alarm["count"] <= 0:
            raise ValueError("alarm.count must be a positive integer")
        if not isinstance(self.alarm["age"], timedelta) or self.alarm["age"] <= timedelta():
            raise ValueError("alarm.age must be a positive timedelta")


_PLACES = tuple(Place(
    name=name, purpose=purpose, reader=reader, why=why, trigger=trigger,
    channel="orchestrator request box, then /fa report line 2",
    alarm={"count": count, "age": age}, capacity={"in": incoming, "out": outgoing},
    removal=removal, person_load={"cap": 0, "form": "multiple-choice"},
) for name, purpose, reader, why, trigger, count, age, incoming, outgoing, removal in (
    ("run-records", "Keep execution evidence", "gate", "Finished records accumulate",
     "run saved", 1000, timedelta(days=30), "run records created", "run records removed",
     "finished records follow their logs' outcome retention"),
    ("logs", "Keep execution output", "gate", "Unread output consumes disk and attention",
     "log opened", 1000, timedelta(days=30), "log files created", "log files removed",
     "inactive issue groups expire by outcome retention or the log size cap"),
    ("held-runs", "Keep work awaiting its reader", "orchestrator", "Unanswered holds stop work",
     "run held, blocked, or still occupying a closed issue's slot", 3, timedelta(minutes=15),
     "runs entering a hold", "holds closed, retried, or slots released",
     "leave on close or retry; a closed issue's active slot leaves when released"),
    ("drafts", "Keep test drafts for publication", "orchestrator", "Unpublished drafts accumulate",
     "draft saved", 20, timedelta(days=7), "draft folders created", "draft folders removed",
     "remove when the issue is closed"),
    # #2454: session promises; orch_promise is their only writer and moves each one
    # waiting -> due -> late with its own per-promise alarm. This place's limits are the backstop.
    ("promises", "Keep 'when X happens, do Y' promises until their owner does Y", "orchestrator",
     "Promises kept only in a conversation are missed",
     "promise recorded (fa orch promise add)", 50, timedelta(days=7), "promises recorded",
     "promises marked done", "fa orch promise done <id>"),
)) + (
    Place(name="requests", purpose="Carry requests to the orchestrator and person decisions to the person",
          reader="orchestrator", why="Unread requests stop work and keep person decisions from the person",
          trigger="request filed or still new or processing", channel="orchestrator request box to the PM",
          alarm={"count": 20, "age": timedelta(hours=24)},
          capacity={"in": "requests filed", "out": "requests done"},
          removal="done requests leave open work and stay as the record their readers remember",
          person_load={"cap": 5, "form": "multiple-choice"}),
    Place(name="notes", purpose="Keep planning notes until their boundary publishes them",
          reader="orchestrator", why="A note waiting at a boundary that never comes is lost planning",
          trigger="note deferred to a boundary", channel="orchestrator request box to the PM",
          alarm={"count": 100, "age": timedelta(hours=24)},
          capacity={"in": "notes deferred", "out": "notes published"},
          removal="leave when the boundary's order publishes them", person_load={"cap": 0, "form": "multiple-choice"}),
    # Waiting idea notes stay in 'notes' too (their boundary may still publish them); this place
    # is the PM's triage: an idea leaves when an order cites it, a candidate confirms it or the PM drops it.
    Place(name="ideas", purpose="Keep waiting idea notes until an order uses them or the PM drops them",
          reader="orchestrator", why="An idea nobody triages is planning nobody does",
          trigger="idea note recorded (fa order note --kind idea)", channel="orchestrator request box to the PM",
          alarm={"count": 20, "age": timedelta(days=7)},
          capacity={"in": "ideas recorded", "out": "ideas cited, confirmed, published or dropped"},
          removal="leave when a published order or confirmed candidate cites them, or fa order drop <id> --reason",
          person_load={"cap": 0, "form": "multiple-choice"}),
    Place(name="lane-lessons", purpose="Keep a lane's pending lessons until its dream folds them",
          reader="lane dream", why="Unfolded lessons are memory no session reads well",
          trigger="lesson appended to a lane's MEMORY.new.md", channel="the lane's dream step",
          alarm={"count": 100, "age": timedelta(days=7)},
          capacity={"in": "pending lessons appended", "out": "lesson ids consumed"},
          removal="the dream folds them and records their ids", person_load={"cap": 0, "form": "multiple-choice"}),
    Place(name="role-lessons", purpose="Keep a role's pending lessons until its curation folds them",
          reader="role curation", why="Unfolded lessons are memory no session reads well",
          trigger="lesson appended to a role's MEMORY.new.md", channel="the role's curation session",
          alarm={"count": 100, "age": timedelta(days=7)},
          capacity={"in": "pending lessons appended", "out": "lesson ids consumed"},
          removal="the curation folds them and records their ids", person_load={"cap": 0, "form": "multiple-choice"}),
    Place(name="decisions", purpose="Turn decisions with no issue yet into draft orders",
          reader="orchestrator", why="A decision nobody orders never happens",
          trigger="fewer running issues than target_concurrency", channel="drafting request in the orchestrator box",
          alarm={"count": 1, "age": timedelta(minutes=1)},
          capacity={"in": "decisions recorded", "out": "decisions ordered or dropped"},
          removal="leave when an issue links them or they are dropped",
          person_load={"cap": 0, "form": "multiple-choice"}),
)
# Places whose trigger starts their own reader (a dream, a curation, a drafting request) instead of
# a PM alarm; their observers set membership through ``members``.
STARTERS = {"lane-lessons", "role-lessons", "decisions"}
OWNERS = {"gate": "orchestrator", "orchestrator": "pm", "pm": "report"}
STATE_FILE = "shelves.sqlite3"
_STORES = {}


def places() -> tuple[Place, ...]:
    """The installed places the gate asks, each created through the same constructor."""
    return _PLACES


def bind(store, *, explicit_clock: bool = False) -> None:
    """Bind path-only log writers to the store's injected clock, without retaining stores."""
    root = store.root.resolve()
    bindings = [(weak, explicit) for weak, explicit in _STORES.get(root, []) if weak() is not None]
    _STORES[root] = [*bindings, (ref(store), explicit_clock)]


def destination(path: Path):
    """The bound store and logical item for a managed destination, including aliases."""
    path = path.resolve()
    for root in path.parents:
        bindings = [(weak(), explicit) for weak, explicit in _STORES.get(root, []) if weak() is not None]
        if not bindings:
            continue
        # An incidental read-only store must not replace an injected fake/project clock.
        store = next((store for store, explicit in reversed(bindings) if explicit), bindings[-1][0])
        relative = path.relative_to(root)
        parts = relative.parts
        if parts[0] == "logs" and len(parts) > 1:
            return store, "logs", relative.as_posix()
        if parts[0] == "runs" and len(parts) == 2 and path.suffix == ".json":
            return store, "run-records", relative.as_posix()
        if parts[0] == "drafts" and len(parts) > 1 and not parts[1].startswith("."):
            return store, "drafts", "/".join(parts[:2])
    return None


def _read(store) -> dict:
    path = store.root / STATE_FILE
    if not path.exists():
        return {"items": {}, "events": [], "episodes": {}}
    # A briefing is read-only, including when no gate has created the ledger yet.
    with _connection(f"{path.resolve().as_uri()}?mode=ro", uri=True) as connection:
        exists = connection.execute("SELECT name FROM sqlite_master WHERE name = 'ledger'").fetchone()
        row = connection.execute("SELECT state FROM ledger WHERE id = 1").fetchone() if exists else None
    return json.loads(row[0]) if row else {"items": {}, "events": [], "episodes": {}}


@contextmanager
def _connection(path, **kwargs):
    connection = sqlite3.connect(path, timeout=30, **kwargs)
    try:
        yield connection
    finally:
        connection.close()


@contextmanager
def ledger(store):
    """One SQLite transaction protects membership, episodes and the 24-hour event ring.

    SQLite supplies crash-safe commits without rewriting the run-record journal. Only
    membership/episode changes commit; appending to an existing log does not grow the ledger.
    This leaf transaction never takes the state or worktree lock.
    """
    store.root.mkdir(parents=True, exist_ok=True)
    with _connection(store.root / STATE_FILE) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("CREATE TABLE IF NOT EXISTS ledger (id INTEGER PRIMARY KEY, state TEXT NOT NULL)")
        row = connection.execute("SELECT state FROM ledger WHERE id = 1").fetchone()
        state = json.loads(row[0]) if row else {"items": {}, "events": [], "episodes": {}}
        yield state
        cutoff = store._now() - timedelta(hours=24)
        state["events"] = [event for event in state["events"] if datetime.fromisoformat(event["at"]) > cutoff]
        content = json.dumps(state, ensure_ascii=False)
        if row is None or content != row[0]:
            connection.execute("INSERT OR REPLACE INTO ledger VALUES (1, ?)", (content,))
        connection.commit()


def transition(state, store, place: str, key: str, at: str | None) -> None:
    """One item entering/leaving is one event; updates keep the item's current age."""
    members = state["items"].setdefault(place, {})
    if (key in members) != (at is not None):
        state["events"].append({"at": store._timestamp(), "place": place,
                                "direction": "in" if at is not None else "out", "item": key})
    if at is None:
        members.pop(key, None)
    else:
        members[key] = at


def _mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()


def _inventory(store) -> dict[str, dict[str, str]]:
    inventory = {place.name: {} for place in places()}
    issues = {row["number"]: row for row in store.local_issues()}
    # Snapshot expiry must not resolve work: completed history still identifies
    # an unanswered hold until its issue is closed or retried.
    held_candidates = store._newest_finished_runs()
    for path in (store.root / "runs").glob("*.json"):
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except ValueError:
            run = {}
        run = run if isinstance(run, dict) else {}
        at = run.get("finished_at") if run.get("status") == "finished" else None
        inventory["run-records"][f"runs/{path.name}"] = at or run.get("started_at") or _mtime(path)
        if run.get("kind", "issue") != "issue" or not isinstance(run.get("number"), int) or not run.get("started_at"):
            continue
        held_candidates[run["number"]] = run
    for run in held_candidates.values():
        number = run["number"]
        issue = issues.get(number, {})
        closed_slot = issue.get("state") == "closed" and run.get("status") == "running"
        waiting = issue.get("state") == "open" and set(issue.get("labels", ())) & {"blocked", "needs-human"}
        if closed_slot or waiting or (issue.get("state") != "closed" and run.get("status") in {"held", "blocked"}):
            flag = store.load_flag(number)
            inventory["held-runs"][f"{number}:{run['started_at']}"] = (
                flag.flagged_at if flag and waiting else run.get("finished_at") or run["started_at"])
    from foragent import orch_promise

    inventory["promises"] = {row["id"]: row.get("created_at") or store._now().isoformat()
                             for row in orch_promise.promises(store)}
    for path in (store.root / "logs").rglob("*"):
        try:
            if path.is_file():
                inventory["logs"][path.relative_to(store.root).as_posix()] = _mtime(path)
        except FileNotFoundError:
            continue  # A writer removed it during observation.
    for path in (store.root / "drafts").glob("*"):
        if path.is_dir() and not path.name.startswith("."):
            inventory["drafts"][f"drafts/{path.name}"] = _mtime(path)
    from foragent.orch_inbox import watched_elsewhere

    for path in (store.root / "inbox" / "requests").glob("*.json"):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        # Alarms, role signals and promise notices escalate themselves; counting them here would
        # escalate them a second time (an alarm would also keep its own episode open).
        if isinstance(row, dict) and row.get("status") != "done" and not watched_elsewhere(row):
            inventory["requests"][str(row.get("id") or path.stem)] = row.get("created_at") or _mtime(path)
    from foragent import order_notes

    for row in order_notes.pending(store)["notes"]:
        if not row.get("recorded_at"):
            continue
        key = str(row.get("id") or row["entry_id"])
        # A note waits at its boundary until the flush publishes it; an idea's shelf status is apart.
        if row.get("status") == "waiting" or row.get("kind") == "idea" and not row["published"]:
            inventory["notes"][key] = row["recorded_at"]
        if row.get("kind") == "idea" and row["status"] != "cleared":
            inventory["ideas"][key] = row["recorded_at"]
    for name in STARTERS:
        inventory.pop(name, None)
    return inventory


def members(store, place: str, current: dict[str, str]) -> None:
    """Set a starter place's membership; an item keeps the admission time it entered with."""
    with ledger(store) as state:
        previous = state["items"].get(place, {})
        for key in set(previous) - current.keys():
            transition(state, store, place, key, None)
        for key, at in current.items():
            transition(state, store, place, key, previous.get(key, at))


def over_groups(store, place: str) -> dict[str, dict]:
    """Groups (``<group>:<item>`` keys) of a starter place past its count or age limit."""
    limits = next(item for item in places() if item.name == place).alarm
    now = store._now()
    groups: dict[str, list[datetime]] = {}
    for key, at in _read(store)["items"].get(place, {}).items():
        groups.setdefault(key.split(":", 1)[0], []).append(datetime.fromisoformat(at))
    return {group: {"count": len(times), "oldest": min(times).isoformat()} for group, times in groups.items()
            if len(times) > limits["count"] or now - min(times) > limits["age"]}


def _observe(state, store) -> None:
    for place, current in _inventory(store).items():
        previous = state["items"].get(place, {})
        # Draft age is its admission, not the directory mtime changed by later files.
        if place == "drafts":
            current = {key: previous.get(key, at) for key, at in current.items()}
        for key in set(previous) - current.keys():
            transition(state, store, place, key, None)
        for key, at in current.items():
            transition(state, store, place, key, at)


def observe(store) -> None:
    """Observe before recovery so a closed issue's held slot enters before it leaves."""
    with ledger(store) as state:
        _observe(state, store)


def _rows(state, now) -> dict:
    result = {}
    for place in places():
        members = state["items"].get(place.name, {})
        events = [event for event in state["events"] if event["place"] == place.name
                  and now - timedelta(hours=24) < datetime.fromisoformat(event["at"]) <= now]
        oldest = min((datetime.fromisoformat(at) for at in members.values()), default=None)
        result[place.name] = {"count": len(members),
                              "in_24h": sum(event["direction"] == "in" for event in events),
                              "out_24h": sum(event["direction"] == "out" for event in events),
                              "oldest_minutes": None if oldest is None else max(0, int((now - oldest).total_seconds() // 60))}
    return result


def summary(store) -> dict:
    """Read current counts without admitting items or advancing alarms from a briefing."""
    state = _read(store)
    inventory = _inventory(store)
    for key, at in inventory["drafts"].items():
        inventory["drafts"][key] = state["items"].get("drafts", {}).get(key, at)
    starters = {name: state["items"].get(name, {}) for name in STARTERS}
    return _rows({**state, "items": {**inventory, **starters}}, store._now())


def pm_alarms(store) -> list[dict]:
    from foragent import orch_inbox

    return [row for row in orch_inbox.requests(store, pending=True)
            if row.get("from") == "gate" and row.get("kind") == "shelf-alarm" and row.get("to") == "pm"]


def report_alarms(store) -> list[str]:
    episodes = _read(store)["episodes"]
    return [f"Shelf alarm: {place.name}" for place in places()
            if episodes.get(place.name, {}).get("level") == "report"]


def ask(store) -> None:
    """Ask every place each tick; deliver one request per level per over-limit episode."""
    from foragent import orch_events, orch_inbox

    # The tick lock serializes asks. No shelf lock is held while an inbox writer takes state.
    with ledger(store) as state:
        _observe(state, store)
        now = store._now()
        clear, deliver = [], []
        for place in places():
            if place.name in STARTERS:
                continue  # Their trigger starts their reader; no PM alarm.
            members = state["items"].get(place.name, {})
            oldest = min((datetime.fromisoformat(at) for at in members.values()), default=now)
            over = len(members) > place.alarm["count"] or now - oldest > place.alarm["age"]
            episode = state["episodes"].get(place.name)
            if not over:
                if episode:
                    clear.append((place.name, episode))
                continue
            if episode is None:
                episode = {"id": uuid4().hex, "level": OWNERS[place.reader], "at": None}
                state["episodes"][place.name] = episode
            elif (episode["level"] != "report" and episode["at"] is not None
                  and now - datetime.fromisoformat(episode["at"]) > place.alarm["age"]):
                level = OWNERS[episode["level"]]
                episode.update(level=level, at=now.isoformat() if level == "report" else None)
            if episode["level"] != "report":
                deliver.append((place.name, dict(episode), list(members)))
    for name, episode, members in deliver:
        existing = next((row for row in orch_inbox.requests(store)
                         if row.get("episode") == episode["id"] and row.get("to") == episode["level"]), None)
        items = [int(key.split(":")[0]) for key in members] if name == "held-runs" else members
        detail = ", ".join(f"#{item}" for item in items) if name == "held-runs" else f"{len(items)} items"
        request = existing or orch_inbox.add_request(
            store, "gate", f"Shelf alarm: {name}; {detail} exceeds its count or age limit.",
            extra={"kind": "shelf-alarm", "place": name, "to": episode["level"],
                   "episode": episode["id"], "items": items})
        if episode["at"] is None:
            with ledger(store) as state:
                state["episodes"][name]["at"] = request["created_at"]
    for name, episode in clear:
        for row in orch_inbox.requests(store, pending=True):
            if row.get("episode") == episode["id"]:
                orch_inbox.set_request_status(store, row["id"], "done", note="Shelf cleared")
        if not orch_events.has_event(store, None, "shelf-cleared", episode=episode["id"]):
            orch_events.append_event(store, store._now(), None, "shelf-cleared", f"Shelf cleared: {name}",
                                     place=name, episode=episode["id"])
        with ledger(store) as state:
            state["episodes"].pop(name, None)
