"""Orchestrator actions, with GitHub evidence and a local event journal."""

from collections import deque
from collections.abc import Iterable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import math
import os
from pathlib import Path
import re
import shlex
from tempfile import TemporaryDirectory
import time
import xml.etree.ElementTree as ET

from foragent import capacity, orch_inbox, orch_seat
from foragent import run_merge_turn as merge_turn
from foragent.git import GitError, PublicationError
from foragent.kick import kick_tick
from foragent.labels import APPROVED, BLOCKED, NEEDS_HUMAN, READY, RUNNING, issue_lane
from foragent.order import detect_test_commands
from foragent.orch_events import append_event
from foragent.orch_reinstall import after_merge
from foragent.orch_stop import stop  # noqa: F401 - `fa orch stop` (orch_stop.py)
from foragent.processes import claude_session, run_logged
from foragent.records import load_record
from foragent.run_attempt import AttemptFailure
from foragent.run_baseline import _baseline_exclusions, _capture_baseline, _premerge_baseline
from foragent.run_checks import CheckResult, trusts_ci
from foragent.run_previous import retry_marker
from foragent.run_publish import retitle_for_merge
from foragent.runner import RunContext, _pytest_command, junit_node_id, review_only
from foragent.tracker import Comment, Issue, PullRequest, RecordingTracker, TrackerError, pr_issue_number


class OrchError(ValueError):
    """An orchestrator action does not apply to the current issue or PR."""


def _watch_snapshot(ctx: RunContext) -> dict:
    rows = ctx.store.local_issues()
    issues = [row for row in rows if row["state"] == "open"]
    return {
        "blocked": sorted(row["number"] for row in issues if BLOCKED in row["labels"]),
        "needs_human": sorted(row["number"] for row in issues if NEEDS_HUMAN in row["labels"]),
        "merged": sorted(row["number"] for row in rows if row["merged"]),
        "queue_empty": not any(set(row["labels"]) & {APPROVED, READY, RUNNING} for row in issues)
                       and not ctx.store.active_runs(),
    }


def watch(ctx: RunContext, *, interval: float = 5, follow: bool = False,
          seat: dict | None = None) -> Iterator[dict]:
    """Yield an initial snapshot, then only changes to the watched sets/state.

    The persisted snapshot lets a Monitor until-loop invoke this repeatedly
    without receiving the same event. Titles, timestamps and logs are excluded.
    A failed read yields ``{"unknown": true, "error": ...}`` instead of empty
    sets, leaves the persisted snapshot alone, and makes the next good read
    print even when it equals the last known snapshot. With ``seat`` (#962, the
    holder ``fa orch watch --follow`` took) every poll checks, in one locked
    step with its heartbeat, that this lifetime still holds the seat, and
    raises ``orch_seat.SeatHeld`` instead of writing once it does not.
    With ``follow``, each new inbox request id and each newly answered question
    also yields one event line (#970); ones present at the first read are seen.
    """
    if not math.isfinite(interval) or interval <= 0:
        raise OrchError("interval must be a finite positive number")
    path = ctx.store.root / "orch-watch.json"
    unknown_path = ctx.store.root / "orch-watch-unknown.json"
    last_error = None
    inbox = orch_inbox.FollowEvents(ctx.store) if follow else None  # baseline at start
    session = claude_session()  # The gate's spawn check trusts it past a stale heartbeat.
    # #413: the id its spawn gave this session; writes from a retired session are ignored.
    session_id = os.environ.get("FORAGENT_SESSION_ID")
    def beat():
        ctx.store.write_heartbeat("orchestrator", ctx.now(), session=session, session_id=session_id)

    while True:
        try:  # Every read, changed or not, shows the orchestrator is alive.
            if seat is not None:  # #962: the seat check and the heartbeat are one step.
                orch_seat.beat(ctx.store, seat, beat)
            else:
                beat()
        except orch_seat.SeatHeld:
            raise
        except Exception:
            pass  # a heartbeat is best effort; the read below reports state errors
        try:
            snapshot = _watch_snapshot(ctx)
            with ctx.store._state_lock():
                try:
                    previous = json.loads(path.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    previous = None
                recovered = unknown_path.exists()
                changed = recovered or snapshot != previous
                if changed:
                    ctx.store._write_json(path, snapshot)
                if recovered:
                    unknown_path.unlink(missing_ok=True)
            events = inbox.poll(ctx.store) if inbox is not None else []
        except Exception as error:  # an unreadable state must never look like empty sets
            message = str(error) or type(error).__name__
            reading = {"unknown": True, "error": message}
            try:
                with ctx.store._state_lock():
                    ctx.store._write_json(unknown_path, reading)
            except Exception:
                pass  # the in-memory flag below still forces the next good print
            if not follow or message != last_error:
                yield reading
            if not follow:
                return
            last_error = message
            time.sleep(interval)
            continue
        if last_error is not None:
            changed = True
            last_error = None
        if changed:
            yield snapshot
            if not follow:
                return
        yield from events
        time.sleep(interval)


def _issue(ctx: RunContext, n: int) -> Issue:
    issue = RecordingTracker(ctx.tracker, ctx).get_issue(n)
    if issue.is_pull_request or issue.state != "open":
        raise OrchError(f"#{n} must be an open issue")
    run = ctx.store.get_run(n)
    if RUNNING in issue.labels or (run is not None and run.status == "running"):
        raise OrchError(f"issue #{n} is running; wait for its runner to finish")
    return issue


def _text(value: str, name: str) -> str:
    if not value.strip():
        raise OrchError(f"{name} must not be empty")
    return value.strip()


def _event(ctx: RunContext, n: int | None, kind: str, summary: str, *, issues: Iterable[int] = (),
           say: str | None = None) -> dict:
    event = append_event(ctx.store, ctx.now(), n, kind, summary, say)
    if ctx.store.github_wait_until(ctx.now()) is not None and not ctx.store.github_outbox():
        return event
    tracker = RecordingTracker(ctx.tracker, ctx, queue_writes=True)
    for number in ([n] if n is not None else issues):
        tracker.comment(number, "<!-- foragent:orch -->\n" + event["summary"])
    return event


def list_blocked(ctx: RunContext) -> list[dict]:
    """Inspect blocked evidence and audit the lookup, as orch-verbs requires."""
    items = []
    for row in ctx.store.local_issues():
        if row["state"] != "open" or BLOCKED not in row["labels"]:
            continue
        issue = Issue(row["number"], row["title"], row["body"], frozenset(row["labels"]), row["state"])
        comments = [load_record(Comment, item) for item in row["comments"]]
        flags = [(comment, match[1]) for comment in comments
                 if (match := re.fullmatch(r"<!-- foragent:blocked reason=([a-z-]+) -->",
                                           comment.body.splitlines()[0] if comment.body else ""))]
        latest = max(flags, key=lambda item: (datetime.fromisoformat(item[0].created_at), item[0].id), default=None)
        reviews = []
        for path in sorted((ctx.store.root / "runs" / f"issue-{issue.number}").glob("review-*.txt")):
            reviews.append({"path": str(path), "text": path.read_text(encoding="utf-8", errors="replace")})
        log = ctx.store.log_path(issue.number)
        tail = ""
        if log.is_file():
            with log.open(encoding="utf-8", errors="replace") as stream:
                tail = "".join(deque(stream, maxlen=60))
        items.append({"number": issue.number, "title": issue.title, "lane": row["lane"],
                      "reason": latest[1] if latest else None,
                      "blocked_at": latest[0].created_at if latest else None,
                      "details": latest[0].body if latest else "",
                      "reviews": reviews, "log_path": str(log), "log_tail": tail})
    append_event(ctx.store, ctx.now(), None, "list", f"Listed {len(items)} blocked issues")
    return items


def _retry(ctx: RunContext, n: int, hint: str | None) -> None:
    tracker = RecordingTracker(ctx.tracker, ctx, queue_writes=True)
    path = ctx.store.root / "hints" / f"{n}.txt"
    if hint is not None:
        ctx.store._atomic_write(path, hint)
    else:
        path.unlink(missing_ok=True)
    # The next implementer receives the keep-work/defect-class default before any hint.
    ctx.store._atomic_write(retry_marker(ctx.store.root, n), "retry\n")
    if (pr := ctx.tracker.find_pr(f"foragent/issue-{n}")) is not None:
        ctx.tracker.close_pr(pr, "Closed by Foragent orchestrator retry")
    # Establish the destination before clearing the old state, including on errors.
    tracker.add_labels(n, [READY])
    tracker.remove_labels(n, [BLOCKED, NEEDS_HUMAN])


def retry(ctx: RunContext, n: int, hint: str | None = None, say: str | None = None) -> str:
    _issue(ctx, n)
    _retry(ctx, n, hint)
    summary = f"#{n}: queued implementation retry" + (f"; hint: {hint}" if hint else "")
    _event(ctx, n, "retry", summary, say=say)
    kick_tick(ctx.repo_root)
    return summary


def rerun_review(ctx: RunContext, n: int, say: str | None = None) -> str:
    _issue(ctx, n)
    outcome = "interrupted"
    try:
        outcome = review_only(n, ctx)
    finally:
        summary = f"#{n}: reran review on the existing PR; {outcome}"
        _event(ctx, n, "rerun-review", summary, say=say)
    return summary


def resolve(ctx: RunContext, n: int, verdict: str, note: str, say: str | None = None) -> str:
    _issue(ctx, n)
    if verdict not in {"pass", "fail"}:
        raise OrchError("verdict must be pass or fail")
    note = _text(note, "note")
    pr = ctx.tracker.find_pr(f"foragent/issue-{n}")
    if pr is None:
        raise OrchError(f"issue #{n} has no open Foragent PR")
    head = ctx.tracker.pr_head_sha(pr)
    RecordingTracker(ctx.tracker, ctx, queue_writes=True).comment(
        n, f"<!-- foragent:orch-verdict pr={pr} head={head} verdict={verdict} -->\n{note}")
    outcome = "interrupted"
    try:
        if verdict == "fail":
            _retry(ctx, n, note)
            outcome = "queued implementation retry"
        else:
            outcome = review_only(n, ctx, verdict_head=head, repair_ci=False)
            # A review verdict cannot fix failing code or CI. Give the implementer
            # the evidence and queue a fresh attempt rather than strand the issue.
            if outcome in {"checks-failed", "ci-failed"}:
                # The failing commands themselves reach the next implementer from the saved bundle.
                _retry(ctx, n, f"{note}\n\nVerification: {outcome}\nSee {ctx.store.log_path(n)}")
                outcome += "; queued implementation retry"
    finally:
        summary = f"#{n}: resolved review {verdict} for {head}; {outcome}; {note}"
        _event(ctx, n, "resolve", summary, say=say)
        kick_tick(ctx.repo_root)
    return summary


def escalate(ctx: RunContext, n: int, summary: str, tried: str,
             options: list[str] | None = None, recommend: int | None = None, say: str | None = None) -> str:
    _issue(ctx, n)
    summary, tried = _text(summary, "summary"), _text(tried, "tried")
    options = [_text(option, "option") for option in (options or [])]
    if recommend is not None and not 1 <= recommend <= len(options):
        raise OrchError("recommend must be a 1-based option index")
    lines = ["<!-- foragent:needs-human reason=escalated -->", summary, "", "What was tried:", tried]
    if options:
        lines.extend(["", "Options:", *(f"{i}. {option}" + (" (recommended)" if i == recommend else "")
                                           for i, option in enumerate(options, 1))])
    tracker = RecordingTracker(ctx.tracker, ctx, queue_writes=True)
    tracker.comment(n, "\n".join(lines))
    tracker.add_labels(n, [NEEDS_HUMAN])
    tracker.remove_labels(n, [READY])
    event_summary = f"#{n}: escalated to human; {summary}; tried: {tried}"
    _event(ctx, n, "escalate", event_summary, say=say)
    return event_summary


def log(ctx: RunContext) -> list[dict]:
    """Read the last twenty events, then audit this repository-wide lookup."""
    path = ctx.store.root / "orch-events.jsonl"
    with ctx.store._state_lock():
        if path.exists():
            with path.open(encoding="utf-8") as stream:
                # Phase timings (kind=phase) are measurements for `fa orch facts`, not actions.
                events = deque((event for line in stream
                                if (event := json.loads(line)).get("kind") != "phase"), maxlen=20)
                events = list(events)
        else:
            events = []
    _event(ctx, None, "log", f"Read {len(events)} recent orchestrator events")
    return events


def _open_pr(ctx: RunContext, n: int) -> PullRequest:
    pr = next((pr for pr in ctx.tracker.list_prs() if pr.number == n), None)
    if pr is None:
        raise OrchError(f"PR #{n} must be open")
    return pr


@dataclass(frozen=True)
class _Premerge:
    pr: PullRequest
    head: str
    base: str
    report: dict


@dataclass(frozen=True)
class _SuiteRun:
    passed: bool
    failing: frozenset[str] | None  # None: no per-test result to compare


def _suite_process(ctx: RunContext, n: int, command: str, worktree: Path,
                   log_path: Path, label: str, slot: AbstractContextManager) -> CheckResult:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(f"\nPre-merge PR #{n} {label}\n$ {command}\n")
    with slot:
        code, timed_out, output = run_logged(
            ["/bin/sh", "-c", command], cwd=worktree,
            timeout_s=ctx.config.max_runtime_minutes * 60, log_path=log_path, env=capacity.HELD_ENV,
        )
    return CheckResult(command, code, timed_out, output)


def _run_suite(ctx: RunContext, n: int, command: str, worktree: Path, junit: Path,
               log_path: Path, label: str, slot: AbstractContextManager, *, allow_empty: bool = False) -> _SuiteRun:
    """Run one suite inside a slot; pytest also reports its failing node ids."""
    pytest = _pytest_command(command) is not None
    if pytest:
        junit.unlink(missing_ok=True)
        command = (f"{command} --rootdir=. -o junit_family=legacy --junit-prefix= "
                   f"--junitxml={shlex.quote(str(junit.resolve()))}")
    result = _suite_process(ctx, n, command, worktree, log_path, label, slot)
    code, timed_out = result.exit_code, result.timed_out
    passed = code == 0 and not timed_out
    if not pytest or timed_out:
        return _SuiteRun(passed, None)
    try:
        cases = list(ET.parse(junit).iter("testcase"))
    except (OSError, ET.ParseError):
        return _SuiteRun(passed, None)
    failing = frozenset(node_id for case in cases
                        if any(case.find(tag) is not None for tag in ("failure", "error"))
                        and (node_id := junit_node_id(case)) is not None)
    if allow_empty and code == 5 and not cases:
        return _SuiteRun(True, frozenset())
    # Exit 1 is pytest's "some tests failed"; other codes are session failures.
    if code not in (0, 1) or (code == 1 and not failing):
        return _SuiteRun(False, None)
    return _SuiteRun(passed, failing)


def _premerge(ctx: RunContext, n: int) -> _Premerge:
    pr = _open_pr(ctx, n)
    head = ctx.tracker.pr_head_sha(n)
    ctx.git.fetch("origin", pr.base)
    # Fetch the exact PR object as well, including heads outside origin's branches.
    ctx.git._call(["fetch", "origin", head])
    base = ctx.git.ref_sha(f"refs/remotes/origin/{pr.base}")
    log_path = ctx.store.root / "runs" / f"premerge-pr-{n}.log"
    waited_seconds = 0.0

    def wait(acquire):
        nonlocal waited_seconds
        started = ctx.now()
        try:
            return acquire()
        finally:
            waited_seconds += max(0.0, (ctx.now() - started).total_seconds())

    @contextmanager
    def test_slot():
        nonlocal waited_seconds
        started = ctx.now()
        queued = False

        def waiting():
            nonlocal queued
            queued = True

        with capacity.hold_test_slot(ctx.config.test_slots, waiting=waiting):
            if queued:
                waited_seconds += max(0.0, (ctx.now() - started).total_seconds())
            yield

    broken: list[str] = []
    failing_on_base: list[str] = []
    with TemporaryDirectory(prefix=f"foragent-premerge-{n}-") as directory:
        worktree = Path(directory) / "worktree"
        ctx.git.add_detached_worktree(worktree, head)
        try:
            probe = ctx.git.at(worktree)
            conflicts = []
            try:
                probe.check_publication(base)
            except PublicationError as exc:
                conflicts = [f"{path}:{line}" for path, line in exc.markers]
            # Test what merging produces: the PR head combined with the current base.
            merged = True
            try:
                probe._call(["-c", "user.name=foragent", "-c", "user.email=foragent@localhost",
                             "merge", "--no-commit", "--no-ff", base])
            except GitError:
                merge_conflicts = sorted(probe.conflict_paths())
                if not merge_conflicts:
                    raise
                conflicts.extend(merge_conflicts)
                probe._call(["merge", "--abort"])
                merged = False
            commands = ([ctx.config.test_command] if ctx.config.test_command is not None
                        else detect_test_commands(worktree))
            if not commands:
                raise OrchError("no full test command detected; set test_command in .foragent.yml")
            ci_units = trusts_ci(ctx.repo_root, ctx.config)
            excluded = _baseline_exclusions(ctx, base, log_path)
            label = f"head={head} merged with base={base}" if merged else f"head={head} (base merge conflicts)"
            runs = []
            for i, command in enumerate(commands):
                pytest_command = _pytest_command(command)
                acceptance_only = ci_units and pytest_command is not None
                selection = command
                if acceptance_only:
                    selection = f"{pytest_command} {shlex.quote(ctx.config.acceptance_dir)}"
                    selection += "".join(f" {shlex.quote('--ignore=' + name)}" for name in excluded)
                if acceptance_only and not (worktree / ctx.config.acceptance_dir).is_dir():
                    run = _SuiteRun(True, frozenset())
                else:
                    run = _run_suite(ctx, n, selection, worktree, Path(directory) / f"head-{i}.xml",
                                     log_path, label, test_slot(), allow_empty=acceptance_only)
                runs.append((command, run, acceptance_only))
        finally:
            ctx.git.remove_worktree(worktree)
        tests = "pass"
        for i, (command, run, acceptance_only) in enumerate(runs):
            if run.passed and not acceptance_only:
                continue
            if not run.passed and run.failing is None:
                tests = "fail"
                continue
            # D53: failures the base already has do not block; any other failure does.
            head_failing = run.failing or frozenset()
            base_failing = frozenset()
            if acceptance_only:
                try:
                    baseline = _capture_baseline(
                        ctx, command, base, log=log_path, report=Path(directory) / f"base-{i}.xml",
                        check=lambda selection, root: _suite_process(
                            ctx, n, selection, root, log_path, f"base={base}", test_slot()),
                        prepare=lambda: None, wait=wait, required_ids=head_failing,
                    )
                except AttemptFailure:
                    tests = "fail"
                    broken.extend(sorted(head_failing))
                    continue
                if baseline is not None:
                    base_failing = frozenset(baseline.failing_ids)
            else:
                selection = command
                if _pytest_command(command) is not None:
                    saved = _premerge_baseline(ctx, command, base, excluded, head_failing)
                    if saved is not None:
                        base_failing = frozenset(saved["failing_ids"])
                        # Keep units and uncached/locked tests in the original full suite.
                        selection += "".join(f" {shlex.quote('--deselect=' + node)}"
                                             for node in [*saved["collected_ids"], *saved["excluded"]])
                ctx.git.add_detached_worktree(worktree, base)
                try:
                    on_base = _run_suite(ctx, n, selection, worktree, Path(directory) / f"base-{i}.xml",
                                         log_path, f"base={base}", test_slot())
                finally:
                    ctx.git.remove_worktree(worktree)
                base_failing |= on_base.failing or frozenset()
            broken.extend(sorted(head_failing - base_failing))
            failing_on_base.extend(sorted(head_failing & base_failing))
            if head_failing - base_failing:
                tests = "fail"
    ci = ctx.tracker.pr_checks(n)
    report = {"pr": n, "tests": tests, "conflicts": conflicts, "ci": ci,
              "broken": broken, "failing_on_base": failing_on_base,
              "waited_seconds": waited_seconds,
              "ready": tests == "pass" and not conflicts and ci in {"pass", "none"}}
    return _Premerge(pr, head, base, report)


def premerge(ctx: RunContext, n: int) -> dict:
    """Check the PR head in isolation without posting or changing GitHub state."""
    return _premerge(ctx, n).report


def _base_note(report: dict) -> str:
    count = len(report["failing_on_base"])
    return f" ({count} failing on the base too)" if count else ""


def _merge_title(ctx: RunContext, pr: PullRequest) -> str:
    """The issue's current title decides the squash subject; unlinked PRs keep theirs."""
    if (number := pr_issue_number(pr.head)) is None:
        return pr.title
    title = f"{ctx.tracker.get_issue(number).title} (#{number})"
    return f"Batch: {title}" if pr.head.startswith("foragent/batch-") else title


def _merge_base_into_pr(ctx: RunContext, n: int, pr: PullRequest, head: str, pending: list[str]) -> None:
    """Bring the base into the PR with lane-memory reconciliation, push it, and stop this merge.

    Both sides changed a lane's MEMORY.new.md; GitHub's merge could keep pending lessons a dream
    already folded. The pushed head is checked again when the merge is run again.
    """
    with TemporaryDirectory(prefix=f"foragent-memory-{n}-") as directory:
        worktree = Path(directory) / "worktree"
        ctx.git.add_detached_worktree(worktree, head)
        try:
            probe = ctx.git.at(worktree)
            probe.merge_base(f"origin/{pr.base}")
            probe._call(["push", f"--force-with-lease=refs/heads/{pr.head}:{head}", "origin",
                         f"HEAD:refs/heads/{pr.head}"])
        finally:
            ctx.git.remove_worktree(worktree)
    raise OrchError(f"PR #{n}: merged {pr.base} into the branch to keep pending lane memory "
                    f"({', '.join(pending)}); run again after its checks pass")


def merge(ctx: RunContext, n: int, *, approved_by: str, at: str | None = None) -> str:
    """Record the human's decision and merge only the freshly checked PR head."""
    approved_by = _text(approved_by, "approved-by")
    at = ctx.now().astimezone(UTC).isoformat() if at is None else _text(at, "at")
    try:
        datetime.fromisoformat(at)
    except ValueError as exc:
        raise OrchError("at must be an ISO8601 timestamp") from exc
    checked = _premerge(ctx, n)
    report = checked.report
    if not report["ready"]:
        broken = f" (broken: {', '.join(report['broken'])})" if report["broken"] else ""
        raise OrchError(f"PR #{n} is not ready: tests {report['tests']}{broken}; "
                        f"conflicts: {', '.join(report['conflicts']) or 'none'}; CI {report['ci']}")
    # A human merge takes the target's FIFO merge turn too (#712). The merge lock is held from the
    # turn claim to the merge, so every freshness check below runs after any lock wait.
    current = _open_pr(ctx, n)
    place = merge_turn.Seat(pr_issue_number(current.head) or n, f"orch-merge {at}", current.base)
    with ctx.store.merge_lock():
        if (holder := merge_turn.claim(ctx, place, checked.head)) is not None:
            raise OrchError(f"PR #{n} waits for the merge turn: #{holder} holds it on {current.base}; "
                            "run again after it finishes")
        try:
            current = _open_pr(ctx, n)
            ctx.git.fetch("origin", current.base)
            if (current.base != checked.pr.base or current.head != checked.pr.head
                    or ctx.git.ref_sha(f"refs/remotes/origin/{current.base}") != checked.base
                    or ctx.tracker.pr_head_sha(n) != checked.head):
                raise OrchError(f"PR #{n} head or base changed during pre-merge; run again")
            if not current.head.startswith("foragent/dream-"):  # A dream PR is checked by its digests below.
                if pending := ctx.git.pending_both_changed(checked.head, f"origin/{current.base}"):
                    _merge_base_into_pr(ctx, n, current, checked.head, pending)
            if current.head.startswith("foragent/dream-"):
                from foragent.orch_dream import memory_path, published_event
                from foragent.orch_dream_publish import memory_moved

                lane = current.head.removeprefix("foragent/dream-")
                if (published := published_event(ctx.store, lane, n)) is None or "inputs" not in published:
                    raise OrchError(f"dream PR #{n} has no recorded dream inputs; close it and the lane "
                                    "dreams again from the base")
                folder = str(Path(memory_path(ctx.config, lane)).parent)
                if moved := memory_moved(ctx.git, current.base, current.head, folder,
                                         published["inputs"], published.get("outputs") or {}):
                    raise OrchError(f"dream PR #{n} is stale: {', '.join(moved)} changed since the dream; "
                                    "close it and the lane dreams again from the base")
            ci = ctx.tracker.pr_checks(n)
            if ci not in {"pass", "none"}:
                raise OrchError(f"PR #{n} is not ready: CI {ci}")
            RecordingTracker(ctx.tracker, ctx, queue_writes=True).comment(
                n, f"Approved by {approved_by} in the orchestrator ({at}). "
                   f"Pre-merge: tests pass{_base_note(report)}, no conflicts, CI {ci}.")
            subject = retitle_for_merge(
                ctx, n, title=_merge_title(ctx, current), current_title=current.title, base=current.base,
                body=lambda: current.body, note=print,
            )
            merge_turn.record_turn(ctx, place, checked.base, "merged")
            try:
                ctx.tracker.merge_pr(n, expected_head_sha=checked.head, **subject)
            except TrackerError:
                merge_turn.record_turn(ctx, place, checked.base, "failed")
                raise
        except BaseException:
            merge_turn.withdraw(ctx, place)
            raise
    summary = (f"PR #{n}: merged {checked.head}; approved by {approved_by} at {at}; "
               f"tests pass{_base_note(report)}, no conflicts, CI {ci}")
    _event(ctx, None, "merge", summary)
    after_merge(ctx, n, current.base)
    if (number := pr_issue_number(current.head)) is not None:
        # GitHub applies closing references only on eligible merges; observe its result.
        try:
            ctx.tracker.get_issue(number)
        except TrackerError as exc:
            print(f"issue #{number} refresh failed after merge: {exc}")
    return summary
