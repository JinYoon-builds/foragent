"""Lane test drafts (#1729): a decided issue still on the placeholder Done when gets its tests drafted.

The gate decides from local facts only. An open, unapproved issue of a configured lane whose
parsed Done when is exactly the order template's placeholder line gets one drafting session:
its own record ``runs/draft-<n>.json`` (kind ``draft``) and its own worker process
(``gate run-draft N``). The worker runs the lane's agent once in a throwaway detached worktree
at the base, then keeps the new acceptance files and ``plan.json`` in ``<state>/drafts/<n>/``
for the orchestrator. It pushes nothing and never touches the issue, its labels or the checkout.

A draft is no lane hold: it never joins the lane queue and starts only in a slot the ready
issues of this tick left free. It counts as an active run, so the shared budget sees it.
The issue's drafting record is its only durable fact (journals are trimmed after 30 days):
outcome ``drafted`` ends its eligibility for good. A session that used an attempt (an error, an
incomplete draft, or a process that died and was finished ``error`` by the tick's dead-run recovery)
lets the issue be drafted again, up to ``MAX_SESSIONS`` attempts in all. A usage limit
(``limit-wait``, which also pauses the executor until its reset) or a transient executor error
(``transient``) uses no attempt (D24).
"""

from __future__ import annotations

from datetime import UTC
import json
import os
from pathlib import Path
import shutil
from typing import TYPE_CHECKING

from foragent.orch_events import append_event

if TYPE_CHECKING:
    from foragent.runner import RunContext
    from foragent.scheduler import TickContext, TickReport
    from foragent.state import StateStore
    from foragent.tracker import Issue

# The Done when line the order template writes before an issue's tests exist (skills/fa-order).
PLACEHOLDER = "false  # placeholder: acceptance tests are written before this issue can be approved"
STARTED = "draft-started"  # Journal audit lines only; eligibility reads the record.
DONE = "draft-done"
DRAFTED = "drafted"  # The record outcome of a complete draft: tests and a plan were kept.
AVAILABILITY = {"limit-wait", "transient"}  # Outcomes that use no attempt (D24).
MAX_SESSIONS = 3  # The first session and at most two more after sessions that ended unfinished.
PLAN = "plan.json"


def drafts_root(store: StateStore) -> Path:
    return store.root / "drafts"


def draft_dir(store: StateStore, number: int) -> Path:
    """Where an issue's draft is kept: outside the checkout, beside the gate's other state."""
    return drafts_root(store) / str(number)


def worktree_path(store: StateStore, number: int) -> Path:
    return store.root / "worktrees" / f"draft-{number}"


def log_path(store: StateStore, number: int) -> Path:
    return store.root / "logs" / f"draft-{number}.log"


def is_placeholder(body: str) -> bool:
    from foragent.issue import parse_body

    return [command.strip() for command in parse_body(body).done_when_commands] == [PLACEHOLDER]


def next_attempts(previous) -> int | None:
    """The attempt count a new session would carry, or None when the issue gets no new session.

    ``previous`` is the issue's drafting record. A running record, one whose processes are still
    owned, or a finished ``drafted`` one starts nothing; availability outcomes keep the count.
    """
    if previous is None:
        return 1
    if previous.status == "running" or previous.process_identities or previous.outcome == DRAFTED:
        return None
    attempts = previous.attempts + (previous.outcome not in AVAILABILITY)
    return attempts if attempts <= MAX_SESSIONS else None


def _lane(issue: Issue, config) -> str:
    from foragent.labels import issue_lane

    return issue_lane(issue.labels, config.lanes)


def _eligible(ctx: TickContext, issue: Issue) -> int | None:
    """The attempt count of a new session for ``issue``, or None when it may not start one now."""
    from foragent.labels import APPROVED

    if issue.state != "open" or getattr(issue, "is_pull_request", False) or APPROVED in issue.labels:
        return None
    lane = _lane(issue, ctx.config)
    if lane == "main" or lane not in ctx.config.lanes or not is_placeholder(issue.body or ""):
        return None
    if ctx.store.executor_paused(_executor(ctx.config, lane), ctx.now()):
        return None
    return next_attempts(ctx.store.get_draft(issue.number))


def _executor(config, lane: str) -> str:
    from foragent.run_implementation import lane_config

    return lane_config(config, lane).executor


def _issues(store: StateStore) -> dict[int, Issue]:
    """One read of the stored issue rows, so a row recorded meanwhile cannot split the view."""
    from foragent.tracker import Issue

    return {row["number"]: Issue(row["number"], row["title"], row["body"], frozenset(row["labels"]), row["state"],
                                 is_pull_request=bool(row.get("is_pull_request")))
            for row in store.local_issues()}


def candidates(ctx: TickContext) -> list[int]:
    """Placeholder issues that may start a drafting session now, by issue number."""
    return sorted(number for number, issue in _issues(ctx.store).items() if _eligible(ctx, issue) is not None)


def startable(ctx: TickContext, sample) -> list[int]:
    """This tick's drafting candidates; each asks the machine budget for one worker."""
    numbers = [] if sample[0] == "critical" else candidates(ctx)
    ctx._draft_demand = len(numbers)
    return numbers


def start(ctx: TickContext, numbers: list[int], budget: int, report: TickReport) -> None:
    """Start drafting sessions in the slots this tick's ready issues left free.

    Each start is checked again under the machine lock (slot and eligibility), as a dream's is.
    """
    from foragent import scheduler

    for number in numbers:
        with scheduler.machine_lock(ctx.store):
            if not scheduler._has_slot(ctx, budget):
                return
            issue = _issues(ctx.store).get(number)
            if issue is None or (attempts := _eligible(ctx, issue)) is None:
                continue
            lane = _lane(issue, ctx.config)
            run = ctx.store.start_draft(number, lane, worktree=worktree_path(ctx.store, number), attempts=attempts,
                                        under_machine_lock=True)
        append_event(ctx.store, ctx.now(), number, STARTED, f"#{number} drafting session started in lane {lane}",
                     lane=lane, started_at=run.started_at, attempts=attempts)
        pid = ctx.launcher(number, kind="draft")
        if pid is not None:
            ctx.store.update_run(run.key, pid=pid)
            report.actions.append(f"#{number} test draft started in lane {lane} (pid={pid})")
            continue
        current = ctx.store.get_draft(number)
        if current is not None and current.status == "running":
            ctx.store.finish_run(run.key, "error", "The launcher started no drafting process.")
            report.actions.append(f"#{number} test draft start failed: no process")


def prompt(issue: Issue, lane: str, acceptance_dir: str) -> str:
    from foragent.issue import parse_body

    goal = parse_body(issue.body or "").goal.strip()
    directory = acceptance_dir.rstrip("/")
    return f"""You are drafting acceptance tests for issue #{issue.number} ("{issue.title}") in the "{lane}" lane.
The issue is decided but its Done when is still the placeholder. Implementation comes later, in another session.

Goal of the issue:
{goal}

The whole issue body:
{issue.body}

Do this:
1. Write draft acceptance tests for the goal as new files under {directory}/ in this directory.
   The tests must use only public seams, and must fail on the current code for a real reason
   (the behavior is missing), not because of a missing module or a collection error.
2. Write {PLAN} at the top of this directory: a JSON object with "id" (a short name for the draft),
   "issues" (a list; one entry for #{issue.number} with its "number", "goal" and the "done_when"
   commands that run your tests) and "notes" (what the tests check and why).
3. Do not change source code or any file outside {directory}/ and {PLAN}. Do not commit, push,
   open a pull request, or edit the issue. This directory is thrown away; the gate keeps your files.

Finish with one sentence saying which tests you drafted.
"""


def _files(directory: Path) -> dict[str, bytes]:
    return {path.relative_to(directory).as_posix(): path.read_bytes()
            for path in sorted(directory.rglob("*")) if path.is_file()} if directory.is_dir() else {}


def _draft(worktree: Path, acceptance_dir: str, before: dict[str, bytes]) -> tuple[dict[str, bytes], str | None]:
    """The session's draft files and what makes the draft incomplete (None when it is complete).

    A complete draft has at least one new or changed test file under the acceptance directory and a
    ``plan.json`` that holds a JSON object. Anything less is no draft: it is not kept, and it uses
    an attempt, so the issue is drafted again.
    """
    directory = acceptance_dir.strip("/")
    after = _files(worktree / directory)
    kept = {f"{directory}/{name}": content for name, content in after.items() if before.get(name) != content}
    tests = [name for name in kept if Path(name).name.startswith("test_") or Path(name).stem.endswith("_test")]
    if not tests:
        return kept, f"no test file under {directory}/"
    plan = worktree / PLAN
    if not plan.is_file():
        return kept, f"no {PLAN}"
    try:
        value = json.loads(plan.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return kept, f"{PLAN} is not JSON ({exc})"
    if not isinstance(value, dict):
        return kept, f"{PLAN} is not a JSON object"
    return {**kept, PLAN: plan.read_bytes()}, None


def _keep(files: dict[str, bytes], target: Path) -> list[str]:
    """Replace ``target`` with ``files`` in one rename; return what was kept."""
    staging = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    shutil.rmtree(staging, ignore_errors=True)
    for name, content in files.items():
        (staging / name).parent.mkdir(parents=True, exist_ok=True)
        (staging / name).write_bytes(content)
    shutil.rmtree(target, ignore_errors=True)
    os.replace(staging, target)
    return sorted(files)


def _unavailable(store: StateStore, config, executor: str, result, windows: dict, now) -> tuple[str, str]:
    """The record outcome of a session the executor could not serve; neither uses an attempt (D24).

    A usage limit pauses the executor until its reset, as an implementation run's does, so no
    drafting session (and no other run) starts it again before then.
    """
    from datetime import timedelta

    from foragent import usage_events, usage_recheck

    message = " ".join((result.last_message or result.log_tail or "").split())[:200]
    if classify(result) == "limit":
        found = windows.get(executor)
        until = usage_events.limit_reset(found, now, now + timedelta(minutes=config.limit_wait_minutes))
        until = usage_recheck.note_limit(store, executor, until, found, now)
        return "limit-wait", f"Drafting executor hit a usage limit; paused until {until.isoformat()}. {message}"
    return "transient", f"Drafting executor was unavailable (transient error). {message}"


def classify(result) -> str:
    from foragent.executors import classify_result

    return classify_result(result)


def run_draft(number: int, ctx: RunContext) -> int:
    """The drafting worker; it owns runs/draft-<n>.json while its process lives."""
    from foragent import usage_events
    from foragent.processes import track_process_group
    from foragent.run_implementation import implementer_setup, lane_config
    from foragent.scheduler_config import tick_config

    store = ctx.store
    ctx.config = config = tick_config(store, ctx.config)  # The configuration the tick admitted it with.
    run = store.get_draft(number)
    if run is None or run.status != "running":
        print(f"foragent: no running draft record for issue #{number}")
        return 1
    key, lane = run.key, run.lane
    worktree, log = worktree_path(store, number), log_path(store, number)
    log.parent.mkdir(parents=True, exist_ok=True)
    created = False
    # The record is finished last, after the worktree cleanup (as a dream's).
    final = ("error", "Drafting worker ended before finishing.")
    try:
        issue = _issues(store).get(number)
        if issue is None:
            issue = ctx.tracker.get_issue(number)
        if worktree.exists():
            ctx.git.remove_worktree(worktree)
        created = True
        ctx.git.add_detached_worktree(worktree, f"origin/{config.base_branch}")
        before = _files(worktree / config.acceptance_dir.strip("/"))
        lane_settings = lane_config(config, lane)
        setup = implementer_setup(lane_settings, lane, "s", worktree)
        executor = ctx.executor_factory(setup.executor)
        text = prompt(issue, lane, config.acceptance_dir) + (f"\n{setup.notes}" if setup.notes else "")
        offset = log.stat().st_size if log.exists() else 0
        size = offset

        def poll() -> None:
            nonlocal size
            current = log.stat().st_size if log.exists() else 0
            if current > size:
                stamp = ctx.now().astimezone(UTC).isoformat()
                store.update_run(key, last_progress_at=stamp, last_activity_at=stamp)
            size = current

        def execute():
            with track_process_group(lambda pgid: store.update_run(key, executor_pgid=pgid),
                                     tree_callback=lambda identities: store.update_run(key, process_identities=identities)):
                return executor.run(worktree=worktree, prompt=text, model=setup.model,
                                    timeout_s=config.max_runtime_minutes * 60, log_path=log, **setup.options)

        windows: dict = {}
        try:
            result = ctx.poll_hook(execute, poll)
        finally:
            windows = usage_events.record_from_log(store, log, offset, ctx.now())
        if result.exit_code not in {None, 0} and classify(result) in {"limit", "transient"}:
            final = _unavailable(store, config, setup.executor, result, windows, ctx.now())
            return 1
        if result.timed_out or result.exit_code not in {None, 0}:
            final = ("error", f"Drafting executor ended without finishing (exit {result.exit_code}).")
            return 1
        files, missing = _draft(worktree, config.acceptance_dir, before)
        if missing is not None:
            final = ("error", f"The drafting session left an incomplete draft: {missing}.")
            return 1
        target = draft_dir(store, number)
        kept = _keep(files, target)
        if log.is_file():
            shutil.copyfile(log, target / "session.log")
        append_event(store, ctx.now(), number, DONE, f"#{number} test draft kept in {target}",
                     lane=lane, path=str(target), files=kept)
        final = (DRAFTED, f"Draft kept in {target}: {', '.join(kept)}")
        return 0
    except Exception as exc:
        final = ("error", f"Drafting failed: {exc}")
        raise
    finally:
        if created:
            try:
                ctx.git.remove_worktree(worktree)
            except Exception:
                pass  # A left-over worktree is removed by the next session before it starts.
        store.finish_run(key, *final)
