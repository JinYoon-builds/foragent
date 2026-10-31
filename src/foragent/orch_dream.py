"""Lane dreams (#189): between work, a lane tidies its own memory.

The gate decides from local facts only when a lane is due: its queue is empty
after at least one merge since its last dream, or ten merges have landed since
then. A dream runs the lane's implementer at tier s in its own worktree on
``foragent/dream-<lane>``; it edits only ``.claude/agent-memory/<dir>/`` and may
propose a cross-project media bundle that the worker copies outside the
repository. A changed memory becomes a normal PR; an unchanged one makes no
GitHub call. Only a completed dream (its ``dream-done`` event) resets the lane; a dream
that opened a PR completes when the tick sees that PR merged.
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from foragent.i18n import text
from foragent.labels import APPROVED, BLOCKED, NEEDS_HUMAN, READY, issue_lane
from foragent.orch_events import append_event
from foragent.state import StateStore

if TYPE_CHECKING:
    from foragent.config import Config
    from foragent.runner import RunContext
    from foragent.scheduler import TickContext, TickReport

DREAM_EVERY_MERGES = 10
DONE = "dream-done"
# A dream that opened a memory PR is complete only once that PR merges (D95); until then
# its runs stay counted, and a closed PR lets the lane dream again.
PUBLISHED = "dream-published"
# A person or the orchestrator asked for a fresh dream (`fa orch dream --redo`); the lane is due
# until a completed dream follows it, whatever its merge count.
REDO = "dream-redo"


def dream_config(config: Config, lane: str) -> Config:
    """The configuration a dream of ``lane`` runs with: that lane's own executor and model win.

    Every dreaming lane is in ``config.lanes``, a lane named main included, so its settings are
    read from there (run_implementation.lane_config treats main as the implicit lane).
    """
    from dataclasses import replace

    settings = config.lanes.get(lane) or {}
    return replace(config, executor=settings.get("executor") or config.executor,
                   executor_model=settings.get("model") if settings.get("model") is not None else config.executor_model)


def memory_dir(config: Config, lane: str) -> str:
    """The lane's agent name when the lane sets one (for example fa-orch), else the lane name."""
    from foragent import lane_memory

    return lane_memory.memory_dir(config, lane)


def memory_path(config: Config, lane: str) -> str:
    from foragent import lane_memory

    return f"{lane_memory.lane_directory(config, lane)}/{lane_memory.CURATED}"


def media_root() -> Path:
    """The cross-project media bundle; it lives outside every repository (decision 2026-10-27)."""
    return Path.home() / ".local/share/foragent/media"


def _done_events(store: StateStore, lane: str) -> list[dict]:
    return [event for event in _lane_events(store, lane) if event.get("kind") == DONE]


def awaiting_pr(store: StateStore, lane: str) -> dict | None:
    """The lane's latest published dream when no completed dream follows it."""
    events = [event for event in _lane_events(store, lane) if event.get("kind") in {DONE, PUBLISHED}]
    return events[-1] if events and events[-1].get("kind") == PUBLISHED else None


def _lane_events(store: StateStore, lane: str) -> list[dict]:
    """The lane's completed, published and redo dream events, in journal order."""
    try:
        lines = (store.root / "orch-events.jsonl").read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    events = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("kind") in {DONE, PUBLISHED, REDO} and event.get("lane") == lane:
            events.append(event)
    return events


def last_dream_at(store: StateStore, lane: str) -> datetime | None:
    """When the lane's latest completed dream was recorded, from the local journal.

    A dream covers the runs finished before its prompt was built, not those during it.
    """
    times = [datetime.fromisoformat(event.get("covered_until") or event["at"]) for event in _done_events(store, lane)]
    return max(times, default=None)


def _redo_at(store: StateStore, lane: str) -> datetime | None:
    """When the lane's latest redo was recorded, from the local journal."""
    redos = [datetime.fromisoformat(event["at"]) for event in _lane_events(store, lane) if event.get("kind") == REDO]
    return max(redos, default=None)


def redo_requested(store: StateStore, lane: str) -> bool:
    """A redo no completed dream has answered yet (derived from the journal).

    Only a dream whose prompt was built after the redo answers it: a completion recorded later
    for an older dream (a dream PR merged before the redo, or a worker that started before it)
    covers runs until an earlier time and leaves the redo standing.
    """
    redo_at = _redo_at(store, lane)
    covered = last_dream_at(store, lane)
    return redo_at is not None and (covered is None or redo_at > covered)


def stale_for_redo(store: StateStore, lane: str, pr: int) -> bool:
    """Dream PR ``pr`` cannot answer the lane's pending redo: no publication of it built after the redo.

    However late it was opened (a worker of unknown liveness that published after the redo, or
    one that published while the redo was being recorded), a PR from an older prompt is stale.
    """
    if not redo_requested(store, lane):
        return False
    published = published_event(store, lane, pr)
    return published is None or datetime.fromisoformat(published["covered_until"]) <= _redo_at(store, lane)


def _runs_since(store: StateStore, lane: str, since: datetime | None) -> list[tuple[Any, str | None]]:
    """The lane's finished issue runs since ``since`` (all before the first dream), oldest first."""
    times = store.finish_times()
    rows = []
    for run in store.read_history():
        if run.kind != "issue" or run.lane != lane:
            continue
        finished = times.get((run.number, run.started_at))
        if since is not None and (finished is None or datetime.fromisoformat(finished) <= since):
            continue
        rows.append((run, finished))
    return sorted(rows, key=lambda row: row[1] or row[0].started_at)


def _queue_empty(config: Config, tracker, lane: str) -> bool:
    for issue in tracker.list_open_issues():
        if issue.is_pull_request or issue.labels & {BLOCKED, NEEDS_HUMAN}:
            continue
        if issue.labels & {APPROVED, READY} and issue_lane(issue.labels, config.lanes) == lane:
            return False
    return True


def dream_due(store: StateStore, config: Config, tracker, now: datetime) -> list[str]:
    """Configured lanes due to dream, each once; a lane with any active run of its own is not due."""
    active = store.active_runs()
    due = []
    from foragent import lane_memory

    for lane in config.lanes:
        if lane_memory.is_role_memory(config, lane):
            continue  # The lane uses a role's memory, which the role curates; no dream rewrites it.
        if any(run.lane == lane for run in active):
            continue  # Its dream is already running, or an issue run of the lane is.
        if (previous := store.get_dream(lane)) is not None and previous.process_identities:
            continue  # An earlier dream still owns processes; the tick reaps them first (D95).
        if redo_requested(store, lane):
            due.append(lane)
            continue
        merges = sum(run.outcome == "merged" for run, _ in _runs_since(store, lane, last_dream_at(store, lane)))
        if merges >= DREAM_EVERY_MERGES or (merges and _queue_empty(config, tracker, lane)):
            due.append(lane)
    return due


class LocalIssues:
    """The issue rows the gate already holds, so the due check reads no GitHub."""

    def __init__(self, store: StateStore):
        self.store = store

    def list_open_issues(self):
        from foragent.tracker import local_issue_models

        return [issue for issue in local_issue_models(self.store) if issue.state == "open"]


def _quota_exhausted(store: StateStore, now: datetime) -> bool:
    """Any recorded usage window at 100% that has not reset; missing usage allows a dream."""
    for record in store.usage().values():
        for window in (record or {}).get("windows") or []:
            reset = window.get("resets_at")
            if float(window.get("used_percent", 0)) >= 100 and not (reset and datetime.fromisoformat(reset) <= now):
                return True
    return False


def _admissible(ctx: TickContext, lanes: list[str] | None = None) -> list[str]:
    """Every start condition, read fresh: due now, no usage window exhausted, implementer not paused.

    The same check estimates this tick's demand before the issue starts and admits
    the dream after them, so a window exhausted or a lane started meanwhile counts.
    """

    now = ctx.now()
    if _quota_exhausted(ctx.store, now):
        return []
    due = dream_due(ctx.store, ctx.config, LocalIssues(ctx.store), now)
    return [lane for lane in (due if lanes is None else [lane for lane in lanes if lane in due])
            if not ctx.store.executor_paused(dream_config(ctx.config, lane).executor, now)]


def startable(ctx: TickContext, sample) -> list[str]:
    """Due lanes whose dream may start this tick; at most one starts, so it asks for one worker."""
    lanes = [] if sample[0] == "critical" else _admissible(ctx)
    ctx._dream_demand = min(1, len(lanes))
    return lanes


def start(ctx: TickContext, lanes: list[str], budget: int, report: TickReport) -> None:
    """Start at most one dream per tick, in a slot the ready issues left free.

    The candidates were computed before this tick's issue starts; every start condition
    is checked again under the machine lock (an issue started in the lane, an exhausted
    usage window, a paused implementer), so none of them is stale.
    """
    from foragent import scheduler
    from foragent.tracker import TrackerError

    if not lanes or not scheduler._has_slot(ctx, budget):
        return
    for lane in lanes:  # One lookup per due lane, before the lock; a lane with an open dream PR waits.
        try:
            open_pr = ctx.tracker.find_pr(f"foragent/dream-{lane}")
        except TrackerError as exc:
            if exc.is_rate_limit:
                scheduler.orch_github.pause(ctx, exc)  # Only the tick records the shared GitHub pause.
            report.actions.append(f"lane {lane} dream not started: {' '.join(str(exc).split())[:200]}")
            return
        if open_pr is not None and stale_for_redo(ctx.store, lane, open_pr):
            # The tick is the only closer of a redo's stale PR, so a PR opened after the command ran
            # is closed too; the fresh dream may start in this same tick.
            try:
                ctx.tracker.close_pr(open_pr, "Replaced by a fresh dream (`fa orch dream --redo`); "
                                              "the lane's notebooks on the base branch are unchanged.")
            except TrackerError as exc:
                if exc.is_rate_limit:
                    scheduler.orch_github.pause(ctx, exc)
                report.actions.append(f"lane {lane} dream PR #{open_pr} not closed for redo: "
                                      f"{' '.join(str(exc).split())[:200]}")
                return
            report.actions.append(f"lane {lane} dream PR #{open_pr} closed for redo; a fresh dream follows")
            open_pr = None
        if open_pr is not None:
            report.actions.append(f"lane {lane} dream PR #{open_pr} is open; the lane does not dream")
            continue
        if (published := awaiting_pr(ctx.store, lane)) is not None:
            try:
                merged = {pr.number for pr in ctx.tracker.list_prs("merged", f"foragent/dream-{lane}")}
            except TrackerError as exc:
                if exc.is_rate_limit:
                    scheduler.orch_github.pause(ctx, exc)
                report.actions.append(f"lane {lane} dream not started: {' '.join(str(exc).split())[:200]}")
                return
            if published.get("pr") in merged:  # The dream completes now; the lane waits for new merges.
                record_done(ctx.store, lane, int(published.get("memories") or 0), ctx.config.language,
                            covered_until=datetime.fromisoformat(published["covered_until"]), pr=published["pr"])
                report.actions.append(f"lane {lane} dream PR #{published['pr']} merged; the dream is complete")
                if not redo_requested(ctx.store, lane):
                    continue
                # That dream predates a redo: the lane still dreams afresh now.
            # Closed without merging: the lane dreams again from the current base.
        # Slot and start conditions again under the machine lock, which is the only lock held: the
        # record is written as an issue start's reservation is, never waiting on the state lock here.
        # A lane no longer admissible (its issue started this tick) gives way to the next due lane.
        with scheduler.machine_lock(ctx.store):
            if not scheduler._has_slot(ctx, budget):
                return
            if lane not in _admissible(ctx, [lane]):
                continue
            ctx.store.start_dream(lane, worktree=worktree_path(ctx.store, lane), under_machine_lock=True)
        break
    else:
        return
    pid = ctx.launcher(kind="dream", lane=lane)
    if pid is not None:  # As for an issue run: the record's pid, with its observed birth identity.
        ctx.store.update_run(f"dream-{lane}", pid=pid)
        report.actions.append(f"lane {lane} started dreaming (pid={pid})")
        return
    run = ctx.store.get_dream(lane)  # A fresh read: an in-process worker may already have finished it.
    if run is not None and run.status == "running":
        # No process and an unfinished record: a failed start; the lane stays due.
        ctx.store.finish_run(run.key, "error", "The launcher started no dream process.")
        report.actions.append(f"lane {lane} dream start failed: no process")
    else:
        report.actions.append(f"lane {lane} dreamed in process")


def worktree_path(store: StateStore, lane: str) -> Path:
    return store.root / "worktrees" / f"dream-{lane}"


def log_path(store: StateStore, lane: str) -> Path:
    return store.root / "logs" / f"dream-{lane}.log"


def record_done(store: StateStore, lane: str, count: int, language: str, at: datetime | None = None, *,
                covered_until: datetime | None = None, pr: int | None = None) -> dict:
    """The completed-dream event: the one fact that resets the lane's eligibility."""
    at = at or store._now()
    say = text("dream_done", language, lane=lane, count=count)
    summary = f"Lane {lane} dreamed; {count} memory lines tidied." + (
        f" Memory PR #{pr} waits for the orchestrator (skills/fa-orchestrator/references/dreams.md)." if pr else "")
    return append_event(store, at, None, DONE, summary, say, lane=lane, memories=count, pr=pr,
                        covered_until=(covered_until or at).astimezone().isoformat())


def record_published(store: StateStore, lane: str, count: int, at: datetime, *,
                     covered_until: datetime, pr: int, inputs: dict, outputs: dict) -> dict:
    """The dream opened memory PR ``pr``; the lane's dream completes only when it merges.

    ``inputs`` and ``outputs`` digest the memory files the dream read on the base and wrote on
    its branch; the merge compares them, never Git ancestry, which a rebase rewrites.
    """
    summary = (f"Lane {lane} dream opened memory PR #{pr} ({count} memory lines); the dream completes "
               "when the orchestrator merges it (skills/fa-orchestrator/references/dreams.md).")
    return append_event(store, at, None, PUBLISHED, summary, lane=lane, memories=count, pr=pr,
                        covered_until=covered_until.astimezone().isoformat(), inputs=inputs, outputs=outputs)


def published_event(store: StateStore, lane: str, pr: int) -> dict | None:
    """The recorded publication of dream PR ``pr``, with the digests of its inputs and outputs."""
    found = [event for event in _lane_events(store, lane) if event.get("kind") == PUBLISHED and event.get("pr") == pr]
    return found[-1] if found else None


def run_dream(lane: str, ctx: RunContext) -> int:
    """The worker runs with the configuration the tick admitted it with, not the checkout's."""
    from foragent.orch_dream_worker import run_dream
    from foragent.scheduler_config import tick_config

    ctx.config = tick_config(ctx.store, ctx.config)
    return run_dream(lane, ctx)


def dreamed_command(store: StateStore, config: Config, lane: str) -> int:
    """`fa lanes dreamed <lane>`: append a completed-dream event; never write the dream record."""
    from foragent.scheduler_config import tick_config

    config = tick_config(store, config)  # The lanes the gate dreams with, from its committed config.
    if lane not in config.lanes:
        print(f"foragent: lane {lane} is not configured", flush=True)
        return 2
    from foragent.scheduler_stop import runner_alive

    run = store.get_dream(lane)  # A fresh read; the record itself is never written here.
    if run is not None and run.status == "running" and run.pid is not None and runner_alive(run) is True:
        print(f"foragent: lane {lane} is dreaming now; wait for it to finish", flush=True)
        return 2
    # Not confirmed alive (gone or unknown): only the completed-dream event is appended;
    # the record stays for the tick, which finishes it once its process is confirmed dead.
    event = record_done(store, lane, 0, config.language)
    print(event["say"])
    return 0


def redo(ctx, lane: str) -> int:
    """`fa orch dream --redo <lane>`: record a redo; the lane is due until a fresh dream completes.

    The command writes only the journal event. The next ordinary tick closes the lane's open dream
    PR when it predates the redo (``stale_for_redo``), then admits the fresh dream through the usual
    open PR, quota, pause and slot checks. Notebooks are never touched.
    """
    from foragent import lane_memory
    from foragent.scheduler_config import tick_config
    from foragent.scheduler_stop import runner_alive

    store = ctx.store
    config = tick_config(store, ctx.config)  # The lanes the gate dreams with, from its committed config.
    if lane not in config.lanes:
        print(f"foragent: lane {lane} is not configured", flush=True)
        return 2
    if lane_memory.is_role_memory(config, lane):
        print(f"foragent: lane {lane} uses a role's memory, which the role curates; it does not dream", flush=True)
        return 2
    run = store.get_dream(lane)
    if run is not None and run.status == "running" and run.pid is not None and runner_alive(run) is True:
        print(f"foragent: lane {lane} is dreaming now; wait for it to finish", flush=True)
        return 2
    summary = (f"lane {lane} dream redo recorded; the next tick closes its stale dream PR, if one is open, "
               "and starts a fresh dream")
    # The state's clock, as record_done uses: a dream whose prompt is built from now on answers it.
    append_event(store, store._now(), None, REDO, summary, lane=lane)
    print(summary, flush=True)
    return 0
