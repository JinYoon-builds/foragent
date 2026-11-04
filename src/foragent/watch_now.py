"""The gate's small, atomic main-screen projection; details stay in local records.

Only the gate publishes now.json, while holding its tick lock. Watch never falls
back to collecting raw state, even before the first tick. No issue text, journal,
run history or scheduling explanations are serialized into this file.
"""

from contextlib import contextmanager
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
    from foragent import watch_scene
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
            return [{"number": item["number"], "phase": item["phase"],
                     "kind": "wait" if item["class"] == "authored" else item["class"]}
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
               "queued": sum(len(lane["next"]) + sum(item["kind"] == "wait" for item in lane["current"])
                             for lane in lanes.values()),
               "questions": watch_scene.person_questions(snapshot),
               "today": done, "total": total, "recent": snapshot.recent,
               "eta_low": iso(low), "eta_high": iso(high), "merges": lane_bubble.latest_merges(snapshot)}
    return {"version": VERSION, "captured_at": iso(snapshot.captured_at),
            "stale": bool(snapshot.status.get("stale")), "as_of": snapshot.status.get("as_of"),
            "gate_age": (snapshot.heartbeats or {}).get("gate"), "lanes": lanes, "summary": summary,
            "roles": {role: {key: fact.get(key) for key in ("alive", "last_called_at")}
                      for role, fact in snapshot.role_facts.items()}}


def publish(ctx) -> None:
    """Called once on exit from an acquired gate tick, including skipped/error ticks."""
    from foragent import status, watch_data

    now = ctx.now()
    facts = watch_data.collect_projection(status.collect(ctx.store, ctx.config, now), ctx.store,
                                          ctx.config, now)
    payload = project(facts, ctx.config)
    path = ctx.store.root / FILENAME
    ctx.store._atomic_write(path, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


@contextmanager
def publishing(ctx):
    try:
        yield
    finally:
        try:
            publish(ctx)
        except (OSError, ValueError):
            # Preserve the previous atomic frame and the scheduling outcome on a failed read/write.
            logging.getLogger(__name__).exception("Could not publish dashboard now.json")


def read(store: StateStore, now: datetime, language: str = "en") -> Snapshot:
    """The complete refresh read: exactly one JSON file, with no raw-state fallback."""
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
    summary = payload.get("summary") or {}
    times = tuple(datetime.fromisoformat(summary[key]) if summary.get(key) else None
                  for key in ("eta_low", "eta_high"))
    lanes = payload["lanes"]
    return Snapshot({"lanes": lanes, "as_of": payload.get("as_of"), "stale": payload.get("stale")},
                    {}, None, summary.get("today", 0), summary.get("total", 0), times[0], [], captured,
                    recent=summary.get("recent", 0), today=summary.get("today", 0),
                    eta_range=times if all(times) else None,
                    heartbeats={"gate": payload["gate_age"]} if payload.get("gate_age") is not None else {},
                    today_done={slug: lane.get("today", 0) for slug, lane in lanes.items()},
                    lane_facts={slug: {"owned_paths": lane.get("paths", [])} for slug, lane in lanes.items()},
                    role_facts=payload.get("roles") or {}, now_view=payload)


def sections():
    """Only the main scene draws a now snapshot; legacy/diagnostic sections cannot do I/O."""
    from foragent.watch_sections import lane_header, lane_bubble, lane_explainers, lane_blocks, lane_footer, settings

    return [lane_header, lane_bubble, lane_explainers, lane_blocks, settings, lane_footer]


def details(target: tuple, snapshot: Snapshot, config: Config, repo_root: Path, *,
            now: datetime, state: dict) -> Snapshot:
    """Load selection facts once per snapshot/selection; existing explainers format every frame."""
    from foragent import watch_data

    store = state.get("store")
    if store is None:
        return snapshot
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
        cached = {"key": key, "snapshot": found}
        state["explanation_detail"] = cached
    return cached["snapshot"]
