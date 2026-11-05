"""Opt-in gate-owned publication. Queue entries survive the implementing process.

Workers append immutable admissions; an independent process owns progress under
merge-owner.lock. The scheduler only starts that process and never waits for it.
Merge prerequisites still come from run_publish's fresh locked read.
"""

from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import os
import subprocess
import sys

from foragent import ci_local, ci_mode, orch_github, orch_queue, orch_stop, processes, run_merge_turn as queue, run_publish, runner
from foragent.issue import body_hash, parse_body
from foragent.git import main_write_lock
from foragent.labels import APPROVED, BLOCKED, NEEDS_HUMAN, READY, RUNNING
from foragent.records import load_record
from foragent.run_attempt import AttemptFailure, ExecutorStopped
from foragent.run_base_move import BaseMove, REUSED, move_is_neutral
from foragent.run_checks import _verify_review_head
from foragent.state import Approval
from foragent.state_local import _approval_identity
from foragent.tracker import TrackerError
from foragent.merge_owner_reads import Snapshot


def start(ctx, report):
    """Reserve and transfer the owner lock; never wait for network, Git or checks."""
    if not getattr(ctx.config, "merge_owner", False):
        return
    if (not getattr(ctx.config, "merge_batch", False) and not admissions(ctx.store)
            and not any((ctx.store.root / "merge-owner").glob("*.json"))):
        return
    if orch_stop.active(ctx.store) is not None:
        return  # A project stop starts no owner process; its queued jobs wait for the release (#1491).
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
        if owner.config.merge_owner:
            compact_queue(owner.store)
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
    if not run_publish._policy_allows(inputs.decision, target, inputs.batch_branch,
                                      authorization=ctx.merge_authorization):
        return "pr-open"
    # Evidence already passed on `base`. A newly moved target is work for the
    # owner, not another implementation turn before the worker can hand off.
    if (inputs.status not in {"pass", "none"}
            or inputs.base_sha == base and not inputs.comparison_current):
        raise AttemptFailure("Publication evidence changed before queue admission", target)
    place = queue.seat(ctx, n, target)
    job = {
        "pr": pr, "branch": branch, "target": target, "head": head,
        "base": base, "approval": asdict(approval), "ready_at": ready_at,
        "comparison": asdict(comparison) if comparison is not None else None,
        "authorization": ctx.merge_authorization,
        "phase": "waiting",
    }
    with ctx.store._state_lock():
        if place.key not in queue._waiting(queue._rows(ctx.store), place.target):
            # The durable evidence must exist before the admission becomes visible.
            ctx.store._write_json(_path(ctx, n), {"started_at": place.started_at, **job})
            publication = {key: value for key, value in job.items() if key != "comparison"}
            queue._append(ctx.store, queue._row(ctx, place, "ready", head_sha=head,
                                              merge_owner=True, publication=publication))
    ctx.tracker.remove_labels(n, [RUNNING, READY])
    return "merge-queued"


def _path(ctx, n):
    return ctx.store.root / "merge-owner" / f"{n}.json"


def compact_queue(store):
    """Strip duplicated evidence only after finding it in the matching job.

    Admission/append and compaction share state.lock. Keep a durable backup
    before the store's fsynced temporary-file + replace publishes the rewrite.
    Unchanged lines (including unreadable ones) retain their original bytes.
    """
    path = store.root / queue.QUEUE
    with store._state_lock():
        if not any(isinstance(row.get("publication"), dict)
                   and row["publication"].get("comparison") is not None for row in queue._rows(store)):
            return 0
        with path.open(encoding="utf-8", newline="") as stream:
            original = stream.read()
        lines = []
        stripped = 0
        jobs = {}
        # Match the universal-newline boundaries used by the queue reader.
        for line in StringIO(original, newline=""):
            try:
                row = json.loads(line)
            except ValueError:
                lines.append(line)
                continue
            publication = row.get("publication") if isinstance(row, dict) else None
            if (isinstance(publication, dict) and publication.get("comparison") is not None
                    and isinstance(row.get("n"), int) and "started_at" in row):
                n = row["n"]
                if n not in jobs:
                    try:
                        jobs[n] = json.loads((store.root / "merge-owner" / f"{n}.json").read_text())
                    except (OSError, ValueError):
                        jobs[n] = None
                job = jobs[n]
                if (isinstance(job, dict) and "started_at" in job and job["started_at"] == row["started_at"]
                        and job.get("comparison") is not None):
                    del publication["comparison"]
                    line = json.dumps(row) + "\n"
                    stripped += 1
            lines.append(line)
        if stripped:
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
            store._atomic_write(path.with_name(f"{queue.QUEUE}.bak-{timestamp}"), original)
            store._atomic_write(path, "".join(lines))
            queue.invalidate(path)
        return stripped


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
    if "started_at" in job and job["started_at"] == place.started_at:
        return {**job, "comparison": job.get("comparison")}
    # Queue metadata can reconstruct the job, but never supplies comparison evidence.
    return {**row["publication"], "comparison": None}


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
    actual_head = pr.head_sha or ctx.tracker.pr_head_sha(pr.number)
    if (not pr.merged_at or pr.number != job["pr"] or pr.base != job["target"]
            or pr.head != job["branch"] or actual_head != job["head"]):
        run_publish._hold_changed_head(place.n, ctx, pr.url)
        _end(ctx, place, job, "held")
        return
    fact = {"pr": pr.number, "target": pr.base, "head": actual_head, "merged_at": pr.merged_at}
    _complete_fact(ctx, place, job, fact)


def _complete_fact(ctx, place, job, fact):
    from foragent import run_group_evidence

    # Phase describes the immutable landing, independently of completion I/O.
    # Keep the admission until required side effects finish so reconciliation
    # can replay them without treating a successful landing as a stopped train.
    job.update(merge_fact=fact, phase="merged")
    _save(ctx, place, job)
    base = job.get("merge_base", job["base"])
    if not any(row.get("event") == "turn" and row.get("result") == "merged"
               and row.get("n") == place.n and row.get("started_at") == place.started_at
               for row in queue._rows(ctx.store)):
        queue.record_turn(ctx, place, base, "merged")
    run_group_evidence.record_merge(place.n, ctx, fact["target"], base, fact["head"])
    ctx.store.record_owner_merge(place.n, place.started_at, fact)
    # The admitted head's landing remains true if this issue authorizes follow-up
    # work. Only the admitted run and approval may receive completion side effects.
    with ctx.reads.fresh():
        issue = ctx.tracker.get_issue(place.n)
    saved = ctx.store.load_approval(place.n)
    run = ctx.store.get_run(place.n)
    if (run is not None and run.started_at == place.started_at
            and saved is not None and _approval_identity(saved) == _approval_identity(load_record(Approval, job["approval"]))
            and body_hash(issue.body) == job["approval"]["body_sha"]
            and APPROVED in issue.labels):
        ctx.store.update_local_issue(place.n, merged=True, pr_base=fact["target"])
        ctx.tracker.close_issue(place.n)
        ctx.tracker.remove_labels(place.n, [RUNNING, READY, BLOCKED])
    _end(ctx, place, job, "merged")
    # The merge is complete before optional installation/reporting side effects.
    # A failed hook must not leave a successfully landed train member in merging.
    if fact["target"] == ctx.config.base_branch:
        runner.after_merge(ctx, fact["pr"], fact["target"])


def _reverify(ctx, place, job, git, head):
    n, target = place.n, job["target"]
    approval = load_record(Approval, job["approval"])
    link = f"https://github.com/{ctx.repo}/pull/{job['pr']}"
    if stopped := _verify_review_head(n, ctx, git, parse_body(approval.body), head, link):
        raise AttemptFailure(f"Refreshed head verification: {stopped}", target)
    attempt = ci_mode.review_checks(n, ctx, git, target)  # local_only stays on the machine in either mode.
    if not ci_mode.enabled(ctx, target):
        ci_local.fallback_comparison(attempt, git.ref_sha(f"origin/{target}"))


def may_write_target(ctx, target):
    """Unfinished published trains exclusively reserve the owner's target."""
    from foragent import merge_owner_batch as batch

    if target != ctx.config.base_branch:
        return True
    retired = {(branch, sha) for row in batch.retirements(ctx)
               for branch, sha in row.get("refs", {}).items()}
    refs = ctx.git._call(["ls-remote", "--heads", "origin", f"refs/heads/{batch.PREFIX}*"])
    return all((ref.removeprefix("refs/heads/"), sha) in retired
               for sha, ref in (line.split() for line in refs.splitlines()))


@contextmanager
def _member_worktree(ctx, job):
    ctx.git.fetch("origin", job["target"])
    ctx.git._call(["fetch", "origin", job["head"]])
    with TemporaryDirectory(prefix="merge-owner-", dir=ctx.store.root) as directory:
        root = Path(directory) / "worktree"
        ctx.git.add_detached_worktree(root, job["head"])
        try:
            yield ctx.git.at(root)
        finally:
            ctx.git.remove_worktree(root)


def _ready(ctx, place, job, pr, git, base, comparison):
    """The one final predicate, shared by single and train admissions."""
    approval = load_record(Approval, job["approval"])
    inputs = run_publish._final_merge_inputs(
        place.n, ctx, git, pr.number, job["branch"], job["target"], job["head"],
        approval, job["ready_at"], approved_body_sha=approval.body_sha, comparison=comparison,
    )
    issue = ctx.tracker.get_issue(place.n)
    closed = issue.state == "closed"
    held = bool(issue.labels & {BLOCKED, NEEDS_HUMAN})
    run = ctx.store.get_run(place.n)
    superseded = run is None or run.started_at != place.started_at
    train = getattr(comparison, "substitutes_ci", False)
    ready = (not (closed or held or superseded) and inputs.stop is None and inputs.base_sha == base
             and (may_write_target(ctx, pr.base) or train)
             and inputs.comparison_current and inputs.status in {"pass", "none"}
             and run_publish._policy_allows(inputs.decision, pr.base, inputs.batch_branch,
                                            authorization=job.get("authorization")))
    if ready:
        run_publish._check_publication(git, pr.base, base)
        return True
    if closed or held or superseded:
        _end(ctx, place, job, "superseded" if superseded else "closed" if closed else "held")
    elif inputs.stop:
        inputs.stop()
        _end(ctx, place, job, "held")
    elif inputs.status == "fail":
        raise AttemptFailure("CI failed at final merge recheck", pr.base)
    return False


def _advance(ctx, place, job, pr, comparison=None):
    if not may_write_target(ctx, job["target"]):
        return False
    with _member_worktree(ctx, job) as git:
        n, root = place.n, git.root
        remote = pr.head_sha or ctx.tracker.pr_head_sha(pr.number)
        publishing = job["phase"] == "publishing"
        if remote != job["head"] and not (publishing and remote == job["previous_head"]):
            run_publish._hold_changed_head(n, ctx, pr.url)
            _end(ctx, place, job, "held")
            return False
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
            if not run_publish._policy_allows(inputs.decision, pr.base, inputs.batch_branch,
                                              authorization=job.get("authorization")):
                _end(ctx, place, job, "held")
                return False
        if publishing:
            if remote == job["previous_head"]:
                if not may_write_target(ctx, pr.base):
                    return False
                git.push_head(pr.head, lease=remote)
            job["phase"] = "waiting"
            _save(ctx, place, job)
            return
        base = git.ref_sha(f"origin/{pr.base}")
        job_comparison = load_record(ci_mode.ComparisonEvidence, job["comparison"]) if job.get("comparison") else None
        measured_base = job_comparison.base_sha if job_comparison else job["base"]
        needs_proof = base != job["base"] or measured_base != base
        neutral = needs_proof and ci_mode.enabled(ctx, pr.base) and job_comparison is not None and move_is_neutral(
            git, BaseMove(measured_base, base), ctx=ctx, n=n,
            own_locked=approval.acceptance_blobs, strict=True,
        )
        if neutral:
            if inputs.status == "pending":
                return False
            followed = job_comparison.follow(base)
            # The same proved range also covers a trusted default ref
            # moving from the previously accepted base. Reports and
            # their measured base remain immutable.
            if job_comparison.trusted_sha == (job_comparison.accepted_sha or job_comparison.base_sha):
                followed = replace(followed, trusted_sha=base)
            neutral = followed.current(n, ctx, git, pr.number, job["head"], base, inputs.status)
            if neutral:
                comparison = followed
        if neutral:
            runner._append(ctx.store.log_path(n), f"{REUSED}: {base}")
        if needs_proof and not neutral:
            def before_push(head):
                if (why := orch_stop.active(ctx.store)) is not None:
                    orch_stop.halt(ctx, n, why)
                if not may_write_target(ctx, pr.base):
                    raise ci_mode.MissingComparison("target reserved by merge train")
                # A progress SHA alone is not a Git reachability root.
                ctx.git._call(["update-ref", f"refs/foragent/merge-owner/{n}", head])
                job.pop("merge_base", None)  # The old merge intent names the replaced head/base.
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
            return  # The changed head reaches CI handling on a later tick.
        status = inputs.status
        if comparison:
            if comparison.current(n, ctx, git, pr.number, job["head"], base, status):
                status = "pass"
            else:
                comparison = None
        if status == "pending":
            return False
        if status == "fail":
            logs = ctx.tracker.pr_failed_logs(pr.number)
            run_publish._hold_if_not_started(n, ctx, pr.number, logs)
            raise AttemptFailure(f"CI failed:\n{logs}", pr.base)
        if ci_mode.enabled(ctx, pr.base):
            comparison = comparison or job_comparison
            if not neutral and (comparison is None or not comparison.current(
                    n, ctx, git, pr.number, job["head"], base, status)):
                attempt = SimpleNamespace(n=n, ctx=ctx, git=git, worktree=root,
                                          target_branch=pr.base, baseline=None, approval=approval,
                                          log=ctx.store.log_path(n))
                comparison = ci_mode.require_comparison(attempt, pr.number, job["head"],
                                                       base_sha=base, status=status, wait=False)
            # Batch substitutes are reconstructed from refs, not job-file evidence.
            if not getattr(comparison, "substitutes_ci", False):
                job["comparison"] = asdict(comparison)
        _save(ctx, place, job)
        with ctx.store.merge_lock(), main_write_lock(ctx.git), ctx.reads.fresh():
            if not _ready(ctx, place, job, pr, git, base, comparison):
                return False
            issue = ctx.tracker.get_issue(place.n)
            subject = run_publish.retitle_for_merge(
                ctx, pr.number, title=f"{issue.title} (#{place.n})",
                current_title=pr.title, base=pr.base, body=lambda: pr.body,
                note=lambda line: runner._append(ctx.store.log_path(place.n), line),
            )
            with orch_stop.merging(ctx, n):
                job.update(phase="merging", merge_base=base)
                _save(ctx, place, job)
                ctx.tracker.merge_pr(pr.number, expected_head_sha=job["head"], delete_branch=False, **subject)
        with ctx.reads.fresh():
            _complete(ctx, place, job, ctx.tracker.get_pr(pr.number))
        return True


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
    except ExecutorStopped as exc:
        if exc.outcome != "stopped":
            _end(ctx, place, job, "held")
        return False  # A project stop keeps the job queued for after the release (#1491).
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


def advance_members(ctx, members, comparison=None):
    """Advance normal single admissions, including a discarded batch's members."""
    advanced = False
    for place, job, pr in members:
        ctx.merge_owner_started_at = ctx.now()
        with _track_checks(ctx, place, job):
            advanced |= _try_admission(ctx, place, job, lambda: _advance(ctx, place, job, pr, comparison))
    return advanced


def _reconcile(ctx, place, job):
    if any(processes.recorded_identity_evidence(item["pid"], item["start_time"], fresh=True).alive is not False
           for item in job.get("process_identities", [])):
        return None  # Cleanup was refused or could not establish absence.
    if job.get("merge_fact"):
        _complete_fact(ctx, place, job, job["merge_fact"])
        return None
    ctx.git.fetch("origin", job["target"])
    target = ctx.git.ref_sha(f"origin/{job['target']}")
    # Fetching the target brings its ancestors, but an unlanded admission's
    # head may still exist only on the remote. That is not a reconciliation error.
    try:
        ctx.git._call(["rev-parse", "--verify", "--quiet", f"{job['head']}^{{commit}}"])
    except run_publish.GitError as exc:
        if not (isinstance(exc.__cause__, subprocess.CalledProcessError) and exc.__cause__.returncode == 1):
            raise
        landed = False
    else:
        landed = ctx.git.is_ancestor(job["head"], target)
    if landed:
        # Commit creation can precede the worker and CI. Record when we confirm
        # reachability, independently of the mutable PR head or issue state.
        _complete_fact(ctx, place, job, dict(pr=job["pr"], target=job["target"],
                                            head=job["head"], merged_at=ctx.now().isoformat()))
        return None
    # Missing resources are terminal only at their direct read boundary; a 404
    # from artifacts, comments or labels is not proof the issue/PR was deleted.
    try:
        pr = ctx.tracker.get_pr(job["pr"])
    except TrackerError as exc:
        if not exc.is_not_found:
            raise
        _end(ctx, place, job, "pr-deleted")
        return None
    if pr.merged_at:
        _complete(ctx, place, job, pr)
        return None
    run = ctx.store.get_run(place.n)
    if run is None or run.started_at != place.started_at:
        _end(ctx, place, job, "superseded")
        return None
    try:
        issue = ctx.tracker.get_issue(place.n)
    except TrackerError as exc:
        if not exc.is_not_found:
            raise
        _end(ctx, place, job, "issue-deleted")
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
        try:
            _tick(owner, report)
        except TrackerError as exc:
            if not exc.waits:
                raise
            orch_github.pause(owner, exc)


def _tick(owner, report):
    ctx = owner
    entries = admissions(ctx.store)
    candidates = {}
    for key, row in entries.items():
        if ctx.store.github_wait_until(ctx.now()):
            return
        place = queue.Seat(row["n"], row["started_at"], row["target"])
        run = ctx.store.get_run(place.n)
        if run is not None and run.started_at == place.started_at and run.status == "running":
            continue  # The worker has not finished its cleanup yet.
        job = dict(row["publication"])

        def reconcile():
            job.update(_load(owner, place, row))
            if (pr := _reconcile(owner, place, job)) is not None:
                candidates[key] = (place, job, pr)

        _try_admission(owner, place, job, reconcile)
    if ctx.store.github_wait_until(ctx.now()):
        return
    # Reuse cross-lane priority + stable join order, including legacy holders
    # while old workers drain during migration.
    rows = queue._rows(ctx.store)
    waiting = {key: row for target in {row["target"] for row in entries.values()}
               for key, row in queue._waiting(rows, target).items()}
    turn_order = queue._turn_order(waiting, orch_queue.get_global(ctx.store))
    batch = None
    from foragent import merge_owner_batch

    if getattr(ctx.config, "merge_batch", False):
        batch = merge_owner_batch.prepare(ctx, report, [candidates[k] for k in turn_order if k in candidates])
    else:
        merge_owner_batch.retire(ctx, report)
    if batch is False:
        return
    if batch:
        merge_owner_batch.land(ctx, report, list(candidates.values()), batch)
        return
    for key in turn_order:
        if ctx.store.github_wait_until(ctx.now()):
            return
        run = ctx.store.get_run(key[0])
        live = (run is not None and run.started_at == key[1]
                if waiting[key].get("merge_owner") else queue._live(ctx.store, key))
        if live:
            # Batch preparation can have repaired or held a candidate in this pass.
            if key in candidates and candidates[key][1]["phase"] in {"waiting", "publishing", "merging"}:
                advanced = advance_members(owner, [candidates[key]])
                if ctx.store.github_wait_until(ctx.now()):
                    return
                if advanced:
                    report.actions.append(f"#{key[0]} merge owner advanced")
                    break
            elif not waiting[key].get("merge_owner"):
                break  # A legacy live worker retains its turn.


if __name__ == "__main__":
    run_process(json.loads(sys.argv[1]), int(sys.argv[2]))
