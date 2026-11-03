"""Opt-in gate-owned publication. Queue entries survive the implementing process.

Workers append immutable admissions; an independent process owns progress under
merge-owner.lock. The scheduler only starts that process and never waits for it.
Merge prerequisites still come from run_publish's fresh locked read.
"""

from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import os
import subprocess
import sys

from foragent import ci_local, ci_mode, orch_github, orch_queue, processes, run_merge_turn as queue, run_publish, runner
from foragent.issue import body_hash, parse_body
from foragent.git import main_write_lock
from foragent.labels import APPROVED, BLOCKED, NEEDS_HUMAN, READY, RUNNING
from foragent.records import load_record
from foragent.run_attempt import AttemptFailure, ExecutorStopped
from foragent.run_checks import _verify_review_head
from foragent.state import Approval
from foragent.state_local import _approval_identity
from foragent.tracker import TrackerError
from foragent.merge_owner_reads import Snapshot


def start(ctx, report):
    """Reserve and transfer the owner lock; never wait for network, Git or checks."""
    if not getattr(ctx.config, "merge_owner", False):
        return
    if not admissions(ctx.store) and not any((ctx.store.root / "merge-owner").glob("*.json")):
        return
    lock = ctx.store.lock_acceptance_baseline("merge-owner", blocking=False)
    if lock is None:
        return
    try:
        request = {"repo": ctx.repo, "repo_root": str(ctx.repo_root.resolve()),
                   "store_root": str(ctx.store.root.resolve()), "config": asdict(ctx.config),
                   "committed_config": getattr(ctx, "committed_config", False)}
        with (ctx.store.root / "merge-owner.log").open("ab") as stream:
            subprocess.Popen(
                [sys.executable, "-m", "foragent.merge_owner", json.dumps(request), str(lock.fileno())],
                pass_fds=(lock.fileno(),), stdin=subprocess.DEVNULL, stdout=stream, stderr=stream,
                start_new_session=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
        report.actions.append("merge owner started")
    except Exception as exc:
        report.actions.append(f"merge owner start deferred: {exc}")
    finally:
        # Do not LOCK_UN: the child's descriptor retains the reservation.
        lock.close()


def run_process(request, lock_fd):
    from datetime import UTC, datetime
    from foragent.config import Config
    from foragent.git import GitRepo
    from foragent.scheduler import TickReport
    from foragent.scheduler_config import fetch_base
    from foragent.state import StateStore
    from foragent.tracker import GhTracker

    with os.fdopen(lock_fd, "a"):
        root = Path(request["repo_root"])
        owner = runner.RunContext(request["repo"], root, GhTracker(request["repo"]),
                                  StateStore(Path(request["store_root"])), Config(**request["config"]),
                                  GitRepo(root), lambda: datetime.now(UTC))
        owner.committed_config = request["committed_config"]
        report = TickReport()
        # Reap under the independent owner lock, even during GitHub backoff.
        reap_checks(owner, report)
        if not owner.store.github_wait_until(owner.now()):
            try:
                fetch_base(owner)
                tick(owner, report)
            except TrackerError as exc:
                if not exc.waits:
                    raise
                orch_github.pause(owner, exc)
        for action in report.actions:
            print(action, flush=True)


def admissions(store):
    rows = queue._rows(store)
    targets = {row["target"] for row in rows if row.get("merge_owner")}
    return {key: row for target in targets for key, row in queue._waiting(rows, target).items()
            if row.get("merge_owner")}


def pending(store, n):
    run = store.get_run(n)
    return run is not None and any(key == (n, run.started_at) for key in admissions(store))


def enqueue(ctx, n, git, pr, branch, target, head, base, approval, ready_at, comparison):
    """Handoff only after all worker checks and the lost-test read have passed."""
    # Reuse the complete mutable-input read even at admission; the gate repeats
    # it under the merge lock before publishing any merge.
    authorization = ctx.merge_authorization
    if authorization and (authorization["pr"] != pr or authorization["target"] != target):
        raise ExecutorStopped("held", "Authorization does not name this PR and target")
    inputs = run_publish._final_merge_inputs(
        n, ctx, git, pr, branch, target, head, approval, ready_at,
        approved_body_sha=approval.body_sha, comparison=comparison,
    )
    if inputs.stop:
        return inputs.stop()
    if not run_publish._policy_allows(inputs.decision, authorization=ctx.merge_authorization):
        return "pr-open"
    # Evidence already passed on `base`. A newly moved target is work for the
    # owner, not another implementation turn before the worker can hand off.
    if (inputs.status not in {"pass", "none"}
            or inputs.base_sha == base and not inputs.comparison_current):
        raise AttemptFailure("Publication evidence changed before queue admission", target)
    place = queue.seat(ctx, n, target)
    queue.join(ctx, place, head, merge_owner=True, publication={
        "pr": pr, "branch": branch, "target": target, "head": head,
        "base": base, "approval": asdict(approval), "ready_at": ready_at,
        "comparison": asdict(comparison) if comparison is not None else None,
        "authorization": ctx.merge_authorization,
        "phase": "waiting",
    })
    ctx.tracker.remove_labels(n, [RUNNING, READY])
    return "merge-queued"


def _path(ctx, n):
    return ctx.store.root / "merge-owner" / f"{n}.json"


def _save(ctx, place, job):
    # The subprocess observer and the tick can journal progress concurrently.
    with ctx.store._state_lock():
        ctx.store._write_json(_path(ctx, place.n), {"started_at": place.started_at, **job})


@contextmanager
def _track_checks(ctx, place, job):
    def observed(identities):
        with ctx.store._state_lock():
            known = {(item["pid"], item["start_time"]): item
                     for item in [*job.get("process_identities", []), *identities]}
            job["process_identities"] = list(known.values())
            ctx.store._write_json(_path(ctx, place.n), {"started_at": place.started_at, **job})

    with processes.track_process_group(lambda pgid: None, tree_callback=observed):
        yield


def reap_checks(ctx, report):
    """The owner lock proves the previous owner is gone, including after SIGKILL.

    Reap even withdrawn/disabled admissions, independently of GitHub availability.
    Only recorded birth identities authorize the existing process-tree stopper.
    """
    for path in (ctx.store.root / "merge-owner").glob("*.json"):
        try:
            job = json.loads(path.read_text())
            identities = job.get("process_identities", [])
            if not identities:
                continue
            with processes.recorded_tree(identities):
                count = processes.kill_process_group(identities[0]["pid"])
            job["process_identities"] = [item for item in identities if
                processes.recorded_identity_evidence(item["pid"], item["start_time"], fresh=True).alive is not False]
            ctx.store._write_json(path, job)
            if count:
                report.actions.append(f"#{path.stem} reaped {count} merge owner check processes")
        except Exception as exc:
            report.actions.append(f"#{path.stem} merge owner check cleanup deferred: {exc}")


def _load(ctx, place, row):
    try:
        job = json.loads(_path(ctx, place.n).read_text())
    except FileNotFoundError:
        job = {}
    return job if job.get("started_at") == place.started_at else dict(row["publication"])


def _end(ctx, place, job, phase):
    job["phase"] = phase
    _save(ctx, place, job)
    queue.withdraw(ctx, place)


def _repair_intent(ctx, place, job, detail):
    # Every fallible read and label side effect must follow durable intent.
    job.update(phase="repair", failure=detail)
    _save(ctx, place, job)


def _repair(ctx, place, job, detail):
    from foragent.orch import _retry

    _repair_intent(ctx, place, job, detail)
    with ctx.reads.fresh():
        pr = ctx.tracker.get_pr(job["pr"])
    if pr.merged_at:
        _complete(ctx, place, job, pr)
        return
    with ctx.reads.fresh():
        _retry(ctx, place.n, detail, expected_pr=pr.number, expected_head_sha=job["head"],
               expected_target=job["target"])
    _end(ctx, place, job, "repair-requested")


def close_for_repair(ctx, n, pr_number, head, target, *, branch=None, comment=None):
    """The single owner-mode PR close boundary. Never delete a remote branch.

    An already-closed matching PR still authorizes replay of the remaining retry
    transition. GitHub cannot atomically compare-and-close, so preserve the ref
    even if another actor pushes after this read.
    """
    current = ctx.tracker.get_pr(pr_number)
    actual_head = current.head_sha or ctx.tracker.pr_head_sha(pr_number)
    if (current.merged_at or actual_head != head or current.base != target
            or branch is not None and current.head != branch):
        if n is None:
            raise TrackerError("PR identity changed before cleanup")
        run_publish._hold_changed_head(n, ctx, current.url)
        raise ExecutorStopped("held", "PR identity changed before repair")
    if current.state == "open":
        ctx.tracker.close_pr(pr_number, comment, delete_branch=False)


def _complete(ctx, place, job, pr):
    from foragent import run_group_evidence

    actual_head = pr.head_sha or ctx.tracker.pr_head_sha(pr.number)
    if (not pr.merged_at or pr.number != job["pr"] or pr.base != job["target"]
            or pr.head != job["branch"] or actual_head != job["head"]):
        run_publish._hold_changed_head(place.n, ctx, pr.url)
        _end(ctx, place, job, "held")
        return
    # Persist the one validated GitHub fact before any completion side effect.
    fact = {"pr": pr.number, "target": pr.base, "head": actual_head, "merged_at": pr.merged_at}
    job["merge_fact"] = fact
    _save(ctx, place, job)
    if not any(row.get("event") == "turn" and row.get("result") == "merged"
               and row.get("n") == place.n and row.get("started_at") == place.started_at
               for row in queue._rows(ctx.store)):
        queue.record_turn(ctx, place, job["base"], "merged")
    run_group_evidence.record_merge(place.n, ctx, fact["target"], job["base"], fact["head"])
    ctx.store.record_owner_merge(place.n, place.started_at, fact)
    # The old PR's merge remains true even if this issue now authorizes follow-up
    # work. Only the admitted approval may receive completion side effects.
    with ctx.reads.fresh():
        issue = ctx.tracker.get_issue(place.n)
    saved = ctx.store.load_approval(place.n)
    if (saved is not None and _approval_identity(saved) == _approval_identity(load_record(Approval, job["approval"]))
            and body_hash(issue.body) == job["approval"]["body_sha"]
            and APPROVED in issue.labels):
        ctx.store.update_local_issue(place.n, merged=True, pr_base=fact["target"])
        ctx.tracker.close_issue(place.n)
        ctx.tracker.remove_labels(place.n, [RUNNING, READY, BLOCKED])
    if fact["target"] == ctx.config.base_branch:
        runner.after_merge(ctx, fact["pr"], fact["target"])
    _end(ctx, place, job, "merged")


def _reverify(ctx, place, job, git, head):
    n, target = place.n, job["target"]
    approval = load_record(Approval, job["approval"])
    link = f"https://github.com/{ctx.repo}/pull/{job['pr']}"
    if stopped := _verify_review_head(n, ctx, git, parse_body(approval.body), head, link):
        raise AttemptFailure(f"Refreshed head verification: {stopped}", target)
    attempt = ci_mode.review_checks(n, ctx, git, target)  # local_only stays on the machine in either mode.
    if not ci_mode.enabled(ctx):
        ci_local.fallback_comparison(attempt, git.ref_sha(f"origin/{target}"))


def _advance(ctx, place, job, pr):
    n = place.n
    remote = pr.head_sha or ctx.tracker.pr_head_sha(pr.number)
    publishing = job["phase"] == "publishing"
    if remote != job["head"] and not (publishing and remote == job["previous_head"]):
        run_publish._hold_changed_head(n, ctx, pr.url)
        _end(ctx, place, job, "held")
        return False
    ctx.git.fetch("origin", pr.base)
    ctx.git._call(["fetch", "origin", remote])
    with TemporaryDirectory(prefix="merge-owner-", dir=ctx.store.root) as directory:
        root = Path(directory) / "worktree"
        ctx.git.add_detached_worktree(root, job["head"])
        try:
            git = ctx.git.at(root)
            approval = load_record(Approval, job["approval"])
            # Validate approval, acceptance hashes, policy and PR target before
            # refreshing too. During a resumed push the lease pins the old head.
            if not publishing:
                inputs = run_publish._final_merge_inputs(
                    n, ctx, git, pr.number, job["branch"], job["target"], job["head"],
                    approval, job["ready_at"], approved_body_sha=approval.body_sha,
                )
                if inputs.stop:
                    inputs.stop()
                    _end(ctx, place, job, "held")
                    return False
                if not run_publish._policy_allows(inputs.decision, authorization=job.get("authorization")):
                    _end(ctx, place, job, "held")
                    return False
            if publishing:
                if remote == job["previous_head"]:
                    git.push_head(pr.head, lease=remote)
                job["phase"] = "waiting"
                _save(ctx, place, job)
                return
            base = git.ref_sha(f"origin/{pr.base}")
            if base != job["base"]:
                def before_push(head):
                    # A progress SHA alone is not a Git reachability root.
                    ctx.git._call(["update-ref", f"refs/foragent/merge-owner/{n}", head])
                    job.update(phase="publishing", previous_head=job["head"], head=head,
                               base=base, comparison=None, ready_at=ctx.now().timestamp())
                    _save(ctx, place, job)
                    ctx.reads.clear()  # The following leased push changes the PR fact.

                head, stopped = run_publish._refresh_reviewed_head(
                    n, ctx, git, pr, job["head"], base,
                    lambda head: _reverify(ctx, place, job, git, head), before_push=before_push,
                    prepare=lambda: ci_mode.commit_exclusions(SimpleNamespace(
                        ctx=ctx, git=git, start_ref=base, log=ctx.store.log_path(n),
                        target_branch=pr.base,
                    ), f"Refresh PR #{pr.number} onto {base}"),
                    before_base_changed=lambda: _repair_intent(
                        ctx, place, job, f"Base refresh failed (base-changed); repair the PR on {base}."),
                )
                if stopped:
                    if stopped == "base-changed":
                        _repair(ctx, place, job, f"Base refresh failed ({stopped}); repair the PR on {base}.")
                    else:
                        _end(ctx, place, job, "held")
                    return False
                job.update(phase="waiting", head=head)
                _save(ctx, place, job)
                return  # The changed head needs its own CI round on a later tick.
            status = inputs.status
            if status == "pending":
                return False
            if status == "fail":
                logs = ctx.tracker.pr_failed_logs(pr.number)
                run_publish._hold_if_not_started(n, ctx, pr.number, logs)
                raise AttemptFailure(f"CI failed:\n{logs}", pr.base)
            comparison = None
            if ci_mode.enabled(ctx):
                comparison = load_record(ci_mode.ComparisonEvidence, job["comparison"]) if job["comparison"] else None
                if comparison is None or not comparison.current(n, ctx, git, pr.number, job["head"], base, status):
                    attempt = SimpleNamespace(n=n, ctx=ctx, git=git, worktree=root,
                                              target_branch=pr.base, baseline=None, approval=approval,
                                              log=ctx.store.log_path(n))
                    comparison = ci_mode.require_comparison(attempt, pr.number, job["head"],
                                                           base_sha=base, status=status, wait=False)
                    job["comparison"] = asdict(comparison)
                    _save(ctx, place, job)
            with ctx.store.merge_lock(), main_write_lock(ctx.git), ctx.reads.fresh():
                inputs = run_publish._final_merge_inputs(
                    n, ctx, git, pr.number, job["branch"], job["target"], job["head"],
                    approval, job["ready_at"], approved_body_sha=approval.body_sha, comparison=comparison,
                )
                # Check issue closure here as well as when selecting the queue.
                issue = ctx.tracker.get_issue(n)
                closed = issue.state == "closed"
                ready = (not closed and inputs.stop is None and inputs.base_sha == base
                         and inputs.comparison_current and inputs.status in {"pass", "none"}
                         and run_publish._policy_allows(inputs.decision, authorization=job.get("authorization")))
                if ready:
                    run_publish._check_publication(git, pr.base, base)
                    subject = run_publish.retitle_for_merge(
                        ctx, pr.number, title=f"{issue.title} (#{n})",
                        current_title=pr.title, base=pr.base, body=lambda: pr.body,
                        note=lambda line: runner._append(ctx.store.log_path(n), line),
                    )
                    job["phase"] = "merging"
                    _save(ctx, place, job)
                    ctx.tracker.merge_pr(pr.number, expected_head_sha=job["head"], delete_branch=False, **subject)
            if ready:
                with ctx.reads.fresh():
                    _complete(ctx, place, job, ctx.tracker.get_pr(pr.number))
                return True
            elif closed:
                _end(ctx, place, job, "closed")
            elif inputs.stop:
                inputs.stop()  # Flags/approval invalidation happen outside the lock.
                _end(ctx, place, job, "held")
            elif inputs.status == "fail":
                raise AttemptFailure("CI failed at final merge recheck", pr.base)
            return False
        finally:
            ctx.git.remove_worktree(root)


def _try_admission(ctx, place, job, step):
    """Isolate both reconciliation and advancement, including repair/completion I/O."""
    try:
        try:
            return step() is not False
        except (AttemptFailure, ci_local.LocalComparisonUnavailable) as exc:
            _repair(ctx, place, job, str(exc))
            return False
    except (ci_mode.MissingComparison, ci_mode.StaleComparison):
        return False  # No waiting process or arbitrary healthy-run wait limit.
    except ExecutorStopped:
        _end(ctx, place, job, "held")
        return False
    except Exception as exc:
        if isinstance(exc, TrackerError):
            if exc.waits:
                orch_github.pause(ctx, exc)
            elif job["phase"] == "merging":
                job["merge_failure"] = str(exc)
                _save(ctx, place, job)
        # Reconcile a lost merge response before attempting repair. Other errors
        # retain the admission and must not block unrelated candidates/starts.
        runner._append(ctx.store.log_path(place.n), f"Merge owner deferred: {exc}")
        return False


def advance_members(ctx, members):
    """List boundary for N>1: add combined evidence/preparation here, not workers.

    Only N=1 is implemented. Each member still needs the same final locked read.
    """
    advanced = False
    for place, job, pr in members:
        ctx.merge_owner_started_at = ctx.now()
        with _track_checks(ctx, place, job):
            advanced |= _try_admission(ctx, place, job, lambda: _advance(ctx, place, job, pr))
    return advanced


def _reconcile(ctx, place, job):
    if any(processes.recorded_identity_evidence(item["pid"], item["start_time"], fresh=True).alive is not False
           for item in job.get("process_identities", [])):
        return None  # Cleanup was refused or could not establish absence.
    # Missing resources are terminal only at their direct read boundary; a 404
    # from artifacts, comments or labels is not proof the issue/PR was deleted.
    try:
        pr = ctx.tracker.get_pr(job["pr"])
    except TrackerError as exc:
        if not exc.is_not_found:
            raise
        _end(ctx, place, job, "pr-deleted")
        return None
    try:
        issue = ctx.tracker.get_issue(place.n)
    except TrackerError as exc:
        if not exc.is_not_found:
            raise
        _end(ctx, place, job, "issue-deleted")
        return None
    if pr.merged_at:
        _complete(ctx, place, job, pr)
        return None
    if issue.state == "closed":
        _end(ctx, place, job, "closed")
        return None
    saved = ctx.store.load_approval(place.n)
    if saved is None or _approval_identity(saved) != _approval_identity(load_record(Approval, job["approval"])):
        _end(ctx, place, job, "superseded")
        return None
    if job["phase"] == "repair":
        _repair(ctx, place, job, job["failure"])
        return None
    if pr.state != "open":
        _end(ctx, place, job, "pr-closed")
        return None
    if job.get("merge_failure"):
        _repair(ctx, place, job, f"Merge failed: {job['merge_failure']}")
        return None
    if job["phase"] in {"held", "closed", "repair-requested", "merged", "superseded"}:
        queue.withdraw(ctx, place)
        return None
    if issue.labels & {BLOCKED, NEEDS_HUMAN}:
        _end(ctx, place, job, "held")
        return None
    if issue.labels & {RUNNING, READY}:
        ctx.tracker.remove_labels(place.n, [RUNNING, READY])
    return pr


def tick(ctx, report):
    """One owner pass; called only while the independent owner lock is held."""
    if not getattr(ctx.config, "merge_owner", False):
        return
    reads = Snapshot(ctx.tracker)
    owner = runner.RunContext(ctx.repo, ctx.repo_root, reads, ctx.store, ctx.config,
                              ctx.git, ctx.now)
    owner.reads = reads
    reap_checks(owner, report)
    if ctx.store.github_wait_until(ctx.now()):
        return
    with reads.fresh():
        _tick(owner, report)


def _tick(owner, report):
    ctx = owner
    entries = admissions(ctx.store)
    candidates = {}
    for key, row in entries.items():
        if ctx.store.github_wait_until(ctx.now()):
            return
        place = queue.Seat(row["n"], row["started_at"], row["target"])
        run = ctx.store.get_run(place.n)
        if run is not None and run.status == "running":
            continue  # The worker has not finished its cleanup yet.
        job = dict(row["publication"])

        def reconcile():
            job.update(_load(owner, place, row))
            if run is None or run.started_at != place.started_at:
                _end(owner, place, job, "superseded")
            elif (pr := _reconcile(owner, place, job)) is not None:
                candidates[key] = (place, job, pr)

        _try_admission(owner, place, job, reconcile)
    # Reuse cross-lane priority + stable join order, including legacy holders
    # while old workers drain during migration.
    rows = queue._rows(ctx.store)
    waiting = {key: row for target in {row["target"] for row in entries.values()}
               for key, row in queue._waiting(rows, target).items()}
    for key in queue._turn_order(waiting, orch_queue.get_global(ctx.store)):
        if ctx.store.github_wait_until(ctx.now()):
            return
        run = ctx.store.get_run(key[0])
        live = (run is not None and run.started_at == key[1]
                if waiting[key].get("merge_owner") else queue._live(ctx.store, key))
        if live:
            if key in candidates:
                if advance_members(owner, [candidates[key]]):
                    report.actions.append(f"#{key[0]} merge owner advanced")
                    break
            elif not waiting[key].get("merge_owner"):
                break  # A legacy live worker retains its turn.


if __name__ == "__main__":
    run_process(json.loads(sys.argv[1]), int(sys.argv[2]))
