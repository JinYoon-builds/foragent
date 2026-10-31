"""Recorded lane, waiting and park facts for the lane view, read once per refresh.

Every fact here comes from a record a writer already keeps: the local issue rows, the
run history, the orchestrator journal, approvals and file claims, the machine's budget
history and the meter the refresh already read. Nothing here replays admission
(orch_facts.collect, scheduler _admission), samples the machine again or stores a
scheduling decision; a missing record leaves its fact None or empty.
"""

import json
from datetime import datetime, timedelta
from glob import has_magic
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from foragent.config import Config
from foragent.labels import BLOCKED, NEEDS_HUMAN, RUNNING, WAITING_PERSON, issue_lane
from foragent.state import StateStore
from foragent.status import issue_state, local_group

# The recorded wait phases and the waiting kind each one establishes (None: cause unknown).
PHASE_KINDS: dict[str, str | None] = {
    "overlap-wait": "file-overlap", "test-slot-wait": "capacity", "limit-wait": "quota",
    "baseline-wait": None, "executor-wait": None,
}
HELD = {"waiting-human", "blocked"}


def _time(value: Any) -> datetime | None:
    """A recorded ISO time with a timezone; anything else is no usable time."""
    if isinstance(value, datetime):
        at = value
    elif isinstance(value, str):
        try:
            at = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    return at if at.tzinfo is not None else None


def _iso(value: Any) -> bool:
    """Whether a value is an ISO time at all; park facts compare no times, so no zone is needed."""
    try:
        datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return False
    return True


def journal(store: StateStore) -> list[dict[str, Any]] | None:
    """Every complete journal event in file order; None when the journal is unreadable."""
    try:
        with (store.root / "orch-events.jsonl").open(encoding="utf-8", errors="replace") as stream:
            lines = stream.readlines()
    except FileNotFoundError:
        return []
    except OSError:
        return None
    events = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue  # An append still being written.
        if isinstance(event, dict):
            events.append(event)
    return events


def park_facts(events: list[dict[str, Any]] | None) -> dict[int, dict[str, Any]]:
    """The last valid parked/unparked event per issue (orch_park.is_parked semantics)."""
    found: dict[int, dict[str, Any]] = {}
    for event in events or []:
        if (event.get("kind") in {"parked", "unparked"} and type(event.get("issue")) is int
                and _iso(event.get("at"))):
            found[event["issue"]] = {"parked": event["kind"] == "parked", "at": event["at"]}
    return found


def _reviews(events: list[dict[str, Any]]) -> dict[int, list[tuple[datetime, int, str]]]:
    """Reviewer verdicts by issue as (at, round, verdict), in journal order.

    The same events and verdicts orch_facts_reviews._recorded counts: reviewer rounds only
    (an orchestrator verdict is not a reviewer round) and a SPEC-CONFLICT round's own
    reviewer verdict. Journal order is kept so equal times keep the order they were written.
    """
    found: dict[int, list[tuple[datetime, int, str]]] = {}
    for event in events:
        if (event.get("kind") != "review" or type(event.get("issue")) is not int
                or type(event.get("round")) is not int or not isinstance(event.get("verdict"), str)
                or event.get("source", "reviewer") != "reviewer"):
            continue
        verdict = event["verdict"]
        if verdict == "SPEC-CONFLICT" and isinstance(event.get("review_verdict"), str):
            verdict = event["review_verdict"]
        if (at := _time(event.get("at"))) is not None:
            found.setdefault(event["issue"], []).append((at, event["round"], verdict))
    return found


def _memory(root: Path | None, agent: str | None) -> str | None:
    """The literal newest lesson: the last nonempty line that is not a heading."""
    if root is None or agent is None:
        return None
    try:
        text = (root / ".claude/agent-memory" / agent / "MEMORY.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    lines = [line.strip() for line in text.splitlines()]
    return next((line for line in reversed(lines) if line and not line.startswith("#")), None)


def lane_facts(
    status: dict[str, Any], store: StateStore, config: Config, now: datetime, root: Path | None,
    work: list[tuple] | None, events: list[dict[str, Any]] | None, rows: dict[int, dict[str, Any]],
    history: list | None,
) -> dict[str, dict[str, Any]]:
    """Owned paths, this week's merges and first-pass reviews, and the newest memory per lane.

    ``work`` is progress.merged_work's rows (None when unreadable); the week runs from local
    Monday 00:00 through ``now``. A merge counts for the lane of the recorded run that
    started it; reviews count only inside that run (start <= review <= merge).
    """
    slugs = list(dict.fromkeys([*config.lanes, *status.get("lanes", {})]))
    week: dict[str, list[int]] | None = None
    if work is not None and history is not None:
        local = now.astimezone()
        monday = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        # merged_work starts a merge at a history or active run; both name its lane.
        lanes_at = {(run.number, _time(run.started_at)): run.lane for run in history if run.lane}
        lanes_at.update({(run["number"], _time(run.get("started_at"))): run["lane"]
                         for run in status.get("active_runs", []) if run.get("lane")})
        reviews = _reviews(events) if events is not None else None
        week = {slug: [0, 0, 0] for slug in slugs}  # finished, first pass, reviewed
        counted: set[int] = set()
        for number, _size, start, end in work:
            start, end = _time(start), _time(end)
            if end is None or not monday <= end <= now or number in counted:
                continue
            counted.add(number)
            lane = lanes_at.get((number, start)) or issue_lane(rows.get(number, {}).get("labels", ()), config.lanes)
            if lane not in week:
                continue
            week[lane][0] += 1
            # No usable run start establishes no reviewed execution.
            inside = [item for item in (reviews or {}).get(number, []) if start and start <= item[0] <= end]
            if reviews is None or not inside:
                continue
            week[lane][2] += 1
            first = min(item[1] for item in inside)
            # The first chronological verdict of the lowest round; min() keeps the first of equal times.
            verdict = min((item for item in inside if item[1] == first), key=lambda item: item[0])[2]
            week[lane][1] += verdict == "PASS"
        if reviews is None:
            week = {slug: [counts[0], None, None] for slug, counts in week.items()}
    facts = {}
    for slug in slugs:
        lane = config.lanes.get(slug)
        agent = None
        if isinstance(lane, dict):
            agent = lane.get("agent") if isinstance(lane.get("agent"), str) and lane["agent"].strip() else f"fa-{slug}"
        paths = lane.get("paths") if isinstance(lane, dict) else None
        counts = week.get(slug) if week is not None else None
        facts[slug] = {
            "owned_paths": [str(path) for path in paths] if isinstance(paths, list) else [],
            "week_finished": counts[0] if counts else None,
            "week_first_pass": counts[1] if counts else None,
            "week_reviewed": counts[2] if counts else None,
            "memory_latest": _memory(root, agent),
        }
    return facts


def _number(value: Any) -> float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _budget_history(store: StateStore) -> list[dict[str, Any]]:
    """The machine's budget and memory-hold rows in file order; unreadable rows are skipped."""
    from foragent import capacity

    try:
        lines = (store.root.parent / capacity.HISTORY_FILE).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _memory_held(rows: list[dict[str, Any]], starts: list[datetime]) -> bool:
    """Whether the newest recorded start attempt was held by memory (a memory-critical row).

    The hold ends with recorded evidence: a later run start in this repository, or a later
    budget row whose own sample read normal memory.
    """
    holds = [(at, index) for index, row in enumerate(rows)
             if row.get("rule") == "memory-critical" and (at := _time(row.get("at"))) is not None]
    if not holds:
        return False
    held_at, index = max(holds)
    if any(start > held_at for start in starts):
        return False
    return not any(row.get("pressure") == "normal" and (at := _time(row.get("at"))) is not None
                   and at > held_at for row in rows[index + 1:])


def _load_reduced(rows: list[dict[str, Any]], config: Config, budget: float) -> bool:
    """Whether the budget sits below its normal size because the newest decrease was load."""
    from foragent import capacity, machine_budget

    if config.max_parallel is not None:
        return False  # The project's own ceiling fixes the budget; it is its normal size.
    for row in reversed(rows):
        if row.get("budget") != "parallel":
            continue
        before, after = _number(row.get("from")), _number(row.get("to"))
        if before is None or after is None or after >= before:
            continue
        load = _number(row.get("load_per_cpu"))
        if load is None or load < capacity.OVERLOAD_LOAD:
            return False  # The newest decrease came from a lower ceiling, not from load.
        try:
            normal = machine_budget.values().get("max_parallel") or before
        except (OSError, ValueError):
            normal = before
        return budget < normal
    return False


def _held_by_machine(
    store: StateStore, config: Config, meter: dict[str, Any], starts: list[datetime] | None,
) -> str | None:
    """'machine' when a recorded memory hold or a load-lowered budget holds starts,
    'capacity' when the meter's slots are full at their normal size, else None."""
    rows = _budget_history(store)
    if starts is not None and _memory_held(rows, starts):
        return "machine"
    running, budget = _number(meter.get("running")), _number(meter.get("budget"))
    if running is None or budget is None or budget <= 0 or running < budget:
        return None
    return "machine" if _load_reduced(rows, config, budget) else "capacity"


def _overlap(ctx, number: int, lane: str, parsed) -> tuple[bool, set[str]]:
    """Whether the gate's file rule holds ``parsed`` against the active recorded claims, and
    the concrete waiting paths those claims share (exclusive or not; union-merged files
    are shared without conflict)."""
    from foragent import overlap_files

    peers, _unknown = overlap_files.claims(ctx, exclude=number, measured=False)
    if not parsed.files or not peers:
        return False, set()
    union = overlap_files.recorded_union(ctx.store)
    # Without unknown runs admits checks only files: exclusive reservations, then overlap_per_file.
    held = not overlap_files.admits(ctx, lane, parsed, peers, [], union=union)
    paths = set()
    for path in {*parsed.files, *parsed.exclusive}:
        if has_magic(path):
            continue  # A glob is no concrete path.
        for peer in peers:
            if (overlap_files.any_overlap(ctx, [path], peer.parsed.exclusive)
                    or (path in parsed.exclusive and overlap_files.any_overlap(ctx, [path], peer.files))
                    or (path in parsed.files and path not in union
                        and overlap_files.any_overlap(ctx, [path], peer.files))):
                paths.add(path)
                break
    return held, paths


def waiting_facts(
    status: dict[str, Any], store: StateStore, config: Config, now: datetime, root: Path | None,
    rows: dict[int, dict[str, Any]], meter: dict[str, Any], history: list | None,
) -> dict[int, dict[str, Any]]:
    """Why each waiting open issue waits, from recorded evidence only.

    Open dependencies come first, then the recorded wait phase, then concrete overlap with
    active recorded claims (only under a known checkout root), then the meter's full slots
    for queued work. Held people and blocked issues are left out.
    """
    states = {number: row["state"] for number, row in rows.items()}
    runs = {run["number"]: run for run in status.get("active_runs", [])}
    waiting_for: dict[int, list[int]] = {}
    visible: set[int] = set()  # The queue the status shows (ready work and approved waiting work).
    for lane in status.get("lanes", {}).values():
        if lane.get("issue") is not None:
            waiting_for[lane["issue"]] = [n for n in lane.get("waiting_for") or [] if type(n) is int]
        visible.update(item["number"] for item in lane.get("queued") or []
                       if isinstance(item, dict) and type(item.get("number")) is int)
    machine = None
    ctx = None
    # Unreadable history cannot show a start after a memory hold, so it proves no hold.
    starts = None if history is None else [
        at for run in [*history, *status.get("active_runs", [])]
        if (at := _time(run.get("started_at") if isinstance(run, dict) else run.started_at)) is not None]
    found: dict[int, dict[str, Any]] = {}
    for number, row in sorted(rows.items()):
        if row["state"] != "open":
            continue
        run = runs.get(number)
        labels = set(row.get("labels") or ())
        state = issue_state(labels - {RUNNING}, run is not None, local_group(row))
        if state in HELD or (run is None and {NEEDS_HUMAN, WAITING_PERSON, BLOCKED} & labels):
            continue  # A person's question or a blocked issue is a held fact, not a wait.
        blocking = {dep["number"] for dep in row.get("blocked_by") or []
                    if isinstance(dep, dict) and type(dep.get("number")) is int
                    and states.get(dep["number"], dep.get("state")) != "closed"}
        blocking |= set(waiting_for.get(number, []))
        phase = run.get("phase") if run else None
        waiting_run = run is not None and phase in PHASE_KINDS
        paths: set[str] = set()
        held = False
        if waiting_run and phase == "overlap-wait":
            paths |= {record["file"] for record in run.get("overlap_records") or []
                      if isinstance(record, dict) and isinstance(record.get("file"), str)}
        # The saved approval and the active recorded claims decide file overlap, whatever the
        # issue's labels; an ordinary running issue already holds its files and waits on none.
        if root is not None and (run is None or waiting_run):
            try:
                if ctx is None:
                    from foragent.git import GitRepo

                    ctx = SimpleNamespace(store=store, config=config, repo_root=root, git=GitRepo(root),
                                          case_sensitive=None, now=lambda: now)
                approval = store.load_approval(number)
                if approval is not None:
                    from foragent.issue import parse_body

                    lane = (run or {}).get("lane") or issue_lane(labels, config.lanes)
                    held, shared = _overlap(ctx, number, lane, parse_body(approval.body))
                    paths |= shared
            except Exception:  # noqa: BLE001, S110 - optional evidence; the entry and its dependencies stay.
                pass
        shown = number in visible and run is None
        if not (blocking or waiting_run or held or shown):
            continue  # Ordinary active work, or an issue with no waiting evidence.
        kind = "dependency" if blocking else PHASE_KINDS.get(phase) if waiting_run else None
        if held and kind is None:
            kind = "file-overlap"
        if kind is None and shown:
            # Capacity facts cover the queue the status shows, equally in every lane (v3.facts.05).
            if machine is None:
                machine = [_held_by_machine(store, config, meter, starts)]
            kind = machine[0]
        found[number] = {"kind": kind, "blocking_issues": sorted(blocking), "overlapping_files": sorted(paths)}
    return found


def merged_today(
    merged_at: dict[int, datetime], work: list[tuple] | None, now: datetime,
) -> dict[int, datetime]:
    """Add every merge since local midnight to ``merged_at``, keeping the newer time."""
    midnight = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    result = dict(merged_at)
    for number, *_, end in work or []:
        if (end := _time(end)) is None or not midnight <= end <= now:
            continue
        known = _time(result.get(number))
        if known is None or end > known:
            result[number] = end
    return result
