"""The gate's small, atomic main-screen projection; details stay in local records.

Only the gate publishes now.json, while holding its tick lock: the tick at its end, and
`fa gate watch` once as it opens when no tick holds the lock (`refresh`), so the first
frame counts work recorded since the last tick exactly as `fa gate status` does. The view
itself never collects raw state; it only reads this file. No issue text, journal, run
history or scheduling explanations are serialized into this file.
"""

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
import json
import logging
from pathlib import Path

from foragent.config import Config
from foragent.state import StateStore
from foragent.watch_data import Snapshot

FILENAME = "now.json"
VERSION = 1


def project(snapshot: Snapshot, config: Config) -> dict:
    from foragent import status, watch_scene
    from foragent.watch_sections import lane_bubble

    lanes = {}
    needs = watch_scene.question_numbers(snapshot)
    for row in watch_scene.lane_rows(snapshot.status):
        current, upcoming = watch_scene.lane_items(snapshot, row, needs)
        # Inactive held work belongs in the queue; an owner handoff remains a current phase.
        held = [item for item in current if not item["run"] and item["phase"] != "merge-owner-wait"
                and item["class"] in {"parked", "authored", "wait"}]
        current = [item for item in current if item not in held]
        upcoming = upcoming + held

        def acorns(items):
            # Every field the acorn draws: its word (phase, class, review round) and, for a question, whose turn it is.
            return [{"number": item["number"], "phase": item["phase"],
                     "kind": "wait" if item["class"] == "authored" else item["class"],
                     "state": item["state"], "asked": item.get("asked"),
                     **({"round": item["round"], "started": (item.get("run") or {}).get("started_at")}
                        if item.get("round") else {})}
                    for item in items]

        kind = watch_scene.category(current, upcoming)
        slug = row["slug"]
        lanes[slug] = {"slug": slug,
                       "state": {"question": "my-turn"}.get(kind, kind),
                       "paths": watch_scene.owned_paths(snapshot, config, slug),
                       "current": acorns(current), "next": acorns(upcoming),
                       "today": watch_scene.today_count(snapshot, slug), "dreaming": bool(row.get("dreaming"))}
    working = [lane for lane in lanes.values() if any(item["kind"] == "working" for item in lane["current"])]
    done, total = lane_bubble.bar_counts(snapshot)
    iso = lambda value: value.isoformat() if value is not None else None
    low, high = snapshot.eta_range or (snapshot.eta, snapshot.eta)
    summary = {"working_lanes": len(working),
               "working": sum(item["kind"] == "working" for lane in working for item in lane["current"]),
               # Waiting its turn is what `fa gate status` counts as queued: held work and work
               # waiting for the orchestrator or a person keep their own lines.
               "queued": queued_count(snapshot.status),
               "questions": watch_scene.person_questions(snapshot),
               "today": done, "total": total, "recent": snapshot.recent,
               "eta_low": iso(low), "eta_high": iso(high), "merges": lane_bubble.latest_merges(snapshot),
               "request": lane_bubble.processing_request(snapshot),
               "requests": lane_bubble.planning_requests(snapshot),
               "orch_off": lane_bubble.orchestrator_off(snapshot, snapshot.captured_at)}
    return {"version": VERSION, "captured_at": iso(snapshot.captured_at),
            # The gate's verdict on whether the numbers may be wrong (status.unknown), so the view
            # warns exactly when the collected frame would, whatever the record lacks.
            "stale": status.unknown(snapshot.status), "as_of": snapshot.status.get("as_of"),
            "gate_age": (snapshot.heartbeats or {}).get("gate"), "lanes": lanes, "summary": summary,
            "roles": {role: {key: fact.get(key) for key in ("alive", "last_called_at")}
                      for role, fact in snapshot.role_facts.items()}}


def queued_count(status_facts: dict) -> int:
    """The queued count `fa gate status` prints, else the lanes' queued listings."""
    from foragent import watch_scene

    value = (status_facts.get("summary") or {}).get("queued")
    if isinstance(value, int):
        return value
    return sum(len(row.get("queued") or ()) for row in watch_scene.lane_rows(status_facts))


def publish(ctx) -> None:
    """Called once on exit from an acquired gate tick, including skipped/error ticks."""
    from foragent import status, watch_data

    now = ctx.now()
    facts = watch_data.collect_projection(status.collect(ctx.store, ctx.config, now), ctx.store,
                                          ctx.config, now)
    payload = project(facts, ctx.config)
    path = ctx.store.root / FILENAME
    ctx.store._atomic_write(path, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def refresh(store: StateStore, config: Config, now: datetime) -> None:
    """Publish once for an opening dashboard when the tick lock is free; a running tick publishes itself.

    Same writer, same lock: the lock holder is the only process that writes now.json.
    A failure keeps the previous frame, as at the end of a tick.
    """
    from types import SimpleNamespace

    try:
        with store.tick_lock() as acquired:
            if acquired:
                publish(SimpleNamespace(store=store, config=config, now=lambda: now))
    except Exception:  # noqa: BLE001 - the dashboard opens on the last published frame instead.
        logging.getLogger(__name__).exception("Could not refresh dashboard now.json")


@contextmanager
def publishing(ctx):
    try:
        yield
    finally:
        try:
            publish(ctx)
        except Exception:  # noqa: BLE001 - the dashboard is an optional view of the tick.
            # Preserve the previous atomic frame and the scheduling outcome on any failed read,
            # projection or write: the dashboard never decides whether the gate tick succeeds.
            logging.getLogger(__name__).exception("Could not publish dashboard now.json")


def read(store: StateStore, now: datetime, language: str = "en") -> Snapshot:
    """The complete refresh read: one JSON file, with no raw-state fallback; only an acorn in
    review also reads its round records (``_fresh_rounds``)."""
    try:
        payload = json.loads((store.root / FILENAME).read_text(encoding="utf-8"))
        if payload.get("version") != VERSION or not isinstance(payload.get("lanes"), dict):
            raise ValueError("unsupported now snapshot")
        captured = datetime.fromisoformat(payload["captured_at"])
        if captured.tzinfo is None:
            raise ValueError("now snapshot needs an aware capture time")
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        payload = {"lanes": {}, "summary": {}, "pending": True, "stale": True, "as_of": None}
        captured = now
    _fresh_rounds(store, payload["lanes"])
    summary = payload.get("summary") or {}
    times = tuple(datetime.fromisoformat(summary[key]) if summary.get(key) else None
                  for key in ("eta_low", "eta_high"))
    lanes = payload["lanes"]
    # "stale" already carries the verdict; a missing observation time must not warn a second time.
    return Snapshot({"lanes": lanes, "as_of": payload.get("as_of") or "", "stale": payload.get("stale")},
                    {}, None, summary.get("today", 0), summary.get("total", 0), times[0], [], captured,
                    recent=summary.get("recent", 0), today=summary.get("today", 0),
                    eta_range=times if all(times) else None,
                    heartbeats={"gate": payload["gate_age"]} if payload.get("gate_age") is not None else {},
                    today_done={slug: lane.get("today", 0) for slug, lane in lanes.items()},
                    lane_facts={slug: {"owned_paths": lane.get("paths", [])} for slug, lane in lanes.items()},
                    role_facts=payload.get("roles") or {}, now_view=payload)


def _fresh_rounds(store: StateStore, lanes: dict) -> None:
    """Bring each reviewing acorn's round up to the runner's records since the tick.

    A reviewer answers and a round is decided between ticks, so the round published with the
    tick lags; the refresh (never a redraw) reads the journal and the saved review files once,
    and only while an acorn is in review. An unreadable journal keeps the published round.
    """
    from foragent import watch_data, watch_lane_facts

    reviewing = [item for lane in lanes.values() if isinstance(lane, dict)
                 for key in ("current", "next") for item in lane.get(key) or ()
                 if isinstance(item, dict) and item.get("round") and type(item.get("number")) is int]
    if not reviewing:
        return
    rounds = watch_data.review_rounds(
        store, [{"number": item["number"], "phase": "reviewing", "started_at": item.get("started")}
                for item in reviewing], watch_lane_facts.journal(store))
    for item in reviewing:
        item["round"] = rounds.get(item["number"], item["round"])


def sections():
    """Only the main scene draws a now snapshot; legacy/diagnostic sections cannot do I/O."""
    from foragent.watch_sections import lane_header, lane_bubble, lane_explainers, lane_blocks, lane_footer, settings

    return [lane_header, lane_bubble, lane_explainers, lane_blocks, settings, lane_footer]


def details(target: tuple, snapshot: Snapshot, config: Config, repo_root: Path, *,
            now: datetime, state: dict) -> Snapshot:
    """Load selection facts once per snapshot/selection; existing explainers format every frame."""
    from foragent import watch_data

    store = state.get("store")
    if store is None or target[0] == "list":
        return snapshot  # A list explains what the published frame itself holds.
    if target[0] == "acorn" and not any(
            item["number"] == target[1] for lane in snapshot.status["lanes"].values()
            for item in lane.get("current", []) + lane.get("next", [])):
        return snapshot
    key = (target, snapshot.captured_at)
    cached = state.get("explanation_detail")
    if cached is None or cached["key"] != key:
        try:
            found = watch_data.collect_details(target, snapshot, store, config, now, repo_root)
        except (OSError, ValueError):
            found = snapshot  # The saved phase still explains an unreadable local record.
        if target[0] == "acorn" and not _draws(found, target[1]):
            # The records moved on since the tick (or never held this acorn): explain the acorn
            # the person clicked as the tick drew it, with whatever the records still say.
            found = replace(found, status=snapshot.status, now_view=snapshot.now_view)
        cached = {"key": key, "snapshot": found}
        state["explanation_detail"] = cached
    return cached["snapshot"]


def _draws(snapshot: Snapshot, number: int) -> bool:
    """Whether ``snapshot`` has an acorn for issue ``number`` in some lane."""
    from foragent import watch_scene

    needs = watch_scene.question_numbers(snapshot)
    return any(item["number"] == number for row in watch_scene.lane_rows(snapshot.status)
               for items in watch_scene.lane_items(snapshot, row, needs) for item in items)
