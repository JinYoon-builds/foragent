"""Test a merge train tip, then publish the whole train with one leased update."""

from contextlib import ExitStack
from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace
import json
import subprocess

from foragent import ci_local, ci_mode, harness_guard, orch_stop, orch_train_pause
from foragent.ci_workflow import EXCLUSIONS_TRAILER, measurement_scope, parse_exclusions
from foragent.ci_local import LocalComparisonUnavailable
from foragent.git import GitError, main_write_lock
from foragent.issue import parse_body
from foragent.records import load_record
from foragent.run_attempt import AttemptFailure, ExecutorStopped
from foragent.run_base_move import BaseMove, strict_move_is_neutral
from foragent.run_baseline import pending_acceptance_exclusions
from foragent.run_checks_approved import acceptance_mismatches
from foragent.state import Approval
from foragent.tracker import TrackerError
from foragent.state_logs import observing

PREFIX = "foragent/batch-merge-"
SPENT = "refs/foragent/batch-spent/"
SILENCE_SECONDS = 60 * 60
# Kept for callers that used the old batch wait allowance.
DEADLINE_SECONDS = SILENCE_SECONDS


def _name(base, k):
    return f"{PREFIX}{base[:8]}-{k}"


def _tree(git, commit):
    return git._call(["show", "-s", "--format=%T", commit]).strip()


def _head_tree_lands(git, base, head):
    """Whether merging ``head`` onto ``base`` lands exactly the tree PR CI measured.

    PR CI measures the head checkout itself, not its merge with the target; a
    report naming the current base proves only the head tree.
    """
    try:
        merged = git._call(["merge-tree", "--write-tree", base, head]).splitlines()[0]
    except GitError:
        return False
    return merged == _tree(git, head)


def _validate_trees(git, base, chain):
    """The ledger authorizes only the Git merge of each recorded pair of parents."""
    before = base
    for n, head, commit, _ in chain:
        if git._call(["show", "-s", "--format=%P", commit]).split() != [before, head]:
            raise ValueError(f"member #{n} merge parents changed")
        expected = git._call(["merge-tree", "--write-tree", before, head]).splitlines()[0]
        if _tree(git, commit) != expected:
            raise ValueError(f"member #{n} merge tree mismatch: {commit}")
        before = commit


def member_paths(git, base, chain):
    """Union each member's real changes, even when later members undo them."""
    from foragent.main_write import changed_paths

    return set().union(*(changed_paths(
        git, git._call(["merge-base", base, head]).strip(), head)
        for _, head, _, _ in chain))


def _accept_target(ctx, base, chain, target, *, reason="target changed"):
    if target != base:
        from foragent import main_write_requests, order_publications
        from foragent.main_write import disjoint_move

        own = member_paths(ctx.git, base, chain)
        move = BaseMove(base, target)
        if strict_move_is_neutral(ctx.git, move, own):
            return
        # Only the owner's durable push intents expand D111's external-move
        # policy. Recover lost push responses from the same immutable commits.
        writes = {row.get(field) for row in main_write_requests.requests(ctx.store)
                  for field in ("landed", "applying")}
        writes.update(row.get("commit") for row in order_publications.requests(ctx.store))
        commits = set(ctx.git._call(["rev-list", f"{base}..{target}"]).split())
        if commits <= writes and disjoint_move(ctx.git, move, own):
            return
        raise ValueError(reason)


def _exclusions(ctx, base, chain, *, own=True):
    """own=False is the single-path PR CI scope; train tip checks still run each member's Done when."""
    return pending_acceptance_exclusions(
        ctx, (base, *(head for _, head, _, _ in chain)), ctx.store.log_path(chain[0][0]),
        own=[n for n, *_ in chain] if own else ())


def _recorded_exclusions(git, tip):
    """The exclusions a train tip was built and measured with, from its own commit.

    None means a tip published before the list was recorded; it is recomputed.
    """
    value = git._call(["show", "-s", f"--format=%(trailers:key={EXCLUSIONS_TRAILER},valueonly)", tip]).strip()
    if not value:
        return None
    try:
        return parse_exclusions(value)
    except (ValueError, TypeError) as exc:
        raise ValueError("tip exclusions trailer invalid") from exc


def retirements(ctx):
    rows = [json.loads(line) for line in ctx.git._call([
        "for-each-ref", "--sort=creatordate", "--format=%(contents:trailers:key=Foragent-Batch,valueonly)",
        SPENT]).splitlines() if line.strip()]
    return sorted(rows, key=lambda row: row.get("sequence", -1))


def _landed(ctx, base, chain):
    ctx.git.fetch("origin", ctx.config.base_branch)
    target = ctx.git.ref_sha(f"origin/{ctx.config.base_branch}")
    return sum(ctx.git.is_ancestor(head, target) for _, head, _, _ in chain)


def _identity(base, chain):
    return sha256(json.dumps([base, [(n, head, started) for n, head, _, started in chain]],
                             separators=(",", ":")).encode()).hexdigest()


def _record_retirement(ctx, identity, candidate, *, reason, **data):
    marker = f"{SPENT}{identity}"
    previous = ctx.git._call(["for-each-ref", "--format=%(objectname)", marker]).strip()
    if previous:
        row = json.loads(ctx.git._call([
            "show", "-s", "--format=%(trailers:key=Foragent-Batch,valueonly)", previous]))
        candidate = row["candidate"]
        reason = row.get("reason", "unknown (legacy retirement did not record a reason)")
    else:
        row = dict(identity=identity, candidate=candidate, sequence=len(retirements(ctx)), **data)
    if "reason" not in row:
        row["reason"] = reason
        ledger = json.dumps(row, separators=(",", ":"))
        retired = ctx.git._call(["-c", "user.name=foragent", "-c", "user.email=foragent@localhost",
                                "commit-tree", _tree(ctx.git, candidate), "-p", previous or candidate,
                                "-m", f"Retire merge batch\n\nForagent-Batch: {ledger}"]).strip()
        ctx.git._call(["update-ref", marker, retired, previous or "0" * len(retired)])
    print(f"merge train {identity[:12]} candidate {candidate[:12]} retired: {reason}", flush=True)
    return reason


def _retire_damaged(ctx, damaged):
    for candidate, branch, detail in damaged:
        identity = sha256(json.dumps([branch, candidate]).encode()).hexdigest()
        _record_retirement(ctx, identity, candidate, reason=f"published prefix ledger unavailable: {branch}: {detail}",
                           members=[], refs={branch: candidate}, spent=[])


def _decode(git, branch, candidate):
    k = int(branch.rsplit("-", 1)[1])
    if k < 1:
        raise ValueError("invalid prefix length")
    chain, commit = [], candidate
    for _ in range(k):
        parents = git._call(["show", "-s", "--format=%P", commit]).split()
        trailer = git._call(["show", "-s", "--format=%(trailers:key=Foragent-Member,valueonly)", commit]).split()
        if len(parents) != 2 or len(trailer) not in {2, 3} or not trailer[0].startswith("#") or trailer[1] != parents[1]:
            raise ValueError("invalid member merge/trailer")
        chain.insert(0, (int(trailer[0].removeprefix("#")), parents[1], commit,
                         trailer[2] if len(trailer) == 3 else None))
        commit = parents[0]
    if branch != _name(commit, k) or len({n for n, *_ in chain}) != k:
        raise ValueError("invalid candidate ledger")
    _validate_trees(git, commit, chain)
    return commit, chain


def _publish(ctx, report, base, chain, k, members):
    branch, prefix = _name(base, k), chain[k - 1][2]
    remote = ctx.git.remote_branch_sha(branch)
    if remote is None:
        from foragent.merge_owner import ci_started

        # Before the push: the tip's CI covers every member up to it.
        ci_started(ctx, prefix, [(n, started) for n, _, _, started in chain[:k]], "batch")
        ctx.git._call(["push", f"--force-with-lease=refs/heads/{branch}:",
                       "origin", f"{prefix}:refs/heads/{branch}"])
    elif remote != prefix:
        raise ValueError("candidate ref changed")
    pr = _prefix_pr(ctx, base, k)
    if remote is None:
        # The published tip now covers these exact heads. Do this once at
        # publication, never on each tick while waiting for the train.
        cancelled = []
        for n, head, _, started in chain[:k]:
            try:
                run_ids = ctx.tracker.cancel_workflow_runs(head, branch=members[n, started].head,
                                                          event="pull_request", best_effort=True)
                # Only successful POSTs authorize coverage of the resulting checks.
                cancelled.append(dict(n=n, head=head, started_at=started, run_ids=run_ids))
                ctx.store._write_json(ctx.store.root / "merge-batch" / f"{prefix}.cancellations.json",
                                      {"members": cancelled})
            except Exception as exc:
                report.actions.append(f"member #{n} PR CI cancellation unavailable: {exc}")
    return pr


def cancelled_runs(ctx, n, head, started, *, tip=None):
    """Successful owner cancellations bound to this admission and, optionally, train."""
    root = ctx.store.root / "merge-batch"
    paths = [root / f"{tip}.cancellations.json"] if tip else root.glob("*.cancellations.json")
    run_ids = set()
    for path in paths:
        try:
            for member in json.loads(path.read_text())["members"]:
                # Legacy intent-only entries contain no proof of a successful POST.
                if (isinstance(member, dict) and
                        (member["n"], member["head"], member["started_at"]) == (n, head, started)):
                    run_ids.update(member["run_ids"])
        except FileNotFoundError:
            continue
    return run_ids


def _ci_result(ctx, tip, result):
    from foragent.merge_owner import ci_result

    ci_result(ctx, tip, result)


def _close(ctx, report, base, chain, reason, used=None, spent=(), tip_pr=None, passed=False, culprit=False):
    from foragent.merge_owner import close_for_repair

    minutes = 0
    with observing("Flow", writer="batch CI duration"):
        minutes = sum(item["report"]["duration_seconds"]
                      for item in ctx.tracker.pr_acceptance_reports(chain[-1][2])) / 60
    members = [dict(n=n, head=head, started_at=started) for n, head, _, started in chain]
    _ci_result(ctx, chain[-1][2], "superseded")  # A tip CI that never reported ends with the train.
    if reason == "tip CI red" and len(chain) == 1:
        spent = chain
    # Persist retirement before deleting the tip, so cleanup can resume after a crash.
    refs = {_name(base, len(chain)): chain[-1][2]} if chain[-1][2] != base else {}
    reason = _record_retirement(ctx, _identity(base, chain), chain[-1][2], reason=reason,
                               base=base, passed=passed, members=members, refs=refs,
                               spent=[dict(n=n, head=h, started_at=s) for n, h, _, s in spent],
                               culprit=bool(culprit and spent))
    from foragent import merge_owner as owner

    # Retirement ends train work even when the following remote cleanup defers.
    with observing("Phase", writer="batch retirement"):
        waiting = owner.admissions(ctx.store)
        for n, _, _, started in chain:
            if (n, started) in waiting:
                with observing("Phase", issue=n):
                    row = waiting[(n, started)]
                    place = owner.queue.Seat(n, started, row["target"])
                    job = owner._load(ctx, place, row)
                    if job["phase"] in {"waiting", "publishing", "merging"}:
                        owner._set_phase(ctx, place, job, "merge-owner-queue")
    if used is None:
        used = _landed(ctx, base, chain)
    with observing("Flow", writer="merge batch retirement"):
        if not any(row["kind"] == "merge-batch" and row["data"].get("members") == members
                   for row in ctx.store.flow_records(include_archives=False)):
            ctx.store.record_flow("merge-batch", members=members, candidate=chain[-1][2],
                                  result="used" if used else "unused", used_members=used,
                                  reason=reason, ci_minutes=minutes)
    for branch, tip in refs.items():
        try:
            if ctx.git.remote_branch_sha(branch) != tip:
                continue
            with ctx.reads.fresh():
                pr = tip_pr if tip_pr is not None else ctx.tracker.find_pr(branch)
                if pr is not None and not ctx.tracker.get_pr(pr).merged_at:
                    close_for_repair(ctx, None, pr, tip, ctx.config.base_branch, branch=branch)
            ctx.tracker.cancel_workflow_runs(tip, branch=branch)
            ctx.git._call(["push", f"--force-with-lease=refs/heads/{branch}:{tip}",
                           "origin", f":refs/heads/{branch}"])
        except Exception as exc:
            if isinstance(exc, TrackerError) and exc.waits:
                raise
            report.actions.append(f"{branch} cleanup deferred: {exc}")


def _discover(ctx, report):
    """Recover immutable member identities from each published tip."""
    refs = ctx.git._call(["ls-remote", "--heads", "origin", f"refs/heads/{PREFIX}*"])
    recovered, damaged = [], []
    retired = {row.get("identity") for row in retirements(ctx)}
    for candidate, ref in (line.split() for line in refs.splitlines()):
        branch = ref.removeprefix("refs/heads/")
        if sha256(json.dumps([branch, candidate]).encode()).hexdigest() in retired:
            continue  # A retained damaged ref has already surrendered its reservation.
        try:
            ctx.git.fetch("origin", branch)
            base, chain = _decode(ctx.git, branch, candidate)
        except Exception as exc:
            if isinstance(exc, TrackerError) and exc.waits:
                raise
            damaged.append((candidate, branch, str(exc)))
            report.actions.append(f"{branch} ledger unavailable: {exc}")
            continue
        recovered.append((base, chain))
    return recovered, damaged


def retire(ctx, report):
    """Retirement remains available when new batching and CI reuse are disabled."""
    recovered, damaged = _discover(ctx, report)
    for base, chain in recovered:
        _close(ctx, report, base, chain, "merge trains disabled")
    _retire_damaged(ctx, damaged)


def _prefix_pr(ctx, base, k):
    branch = _name(base, k)
    pr = ctx.tracker.find_pr(branch)
    if pr is None:
        try:
            pr = ctx.tracker.create_pr(head=branch, base=ctx.config.base_branch,
                                       title=f"Foragent prefix {k}", body=f"Acceptance comparison on {base}.")
        except TrackerError as exc:
            if exc.waits:
                raise
            # Creation may have succeeded before its response/listing arrived.
            # GitHub's unique open PR per head/base makes this retry idempotent.
    return pr


class Evidence:
    infrastructure = ""
    substitutes_ci = True

    def __init__(self, base, chain, pr, born, excluded=None):
        self.base, self.chain, self.pr, self.born = base, chain, pr, born
        # Fixed when the train was built; the moving open-issue set never changes it.
        self.excluded = excluded
        self.reason = None
        self.pending = False
        self.passed = False

    def fresh(self):
        """Per-member evidence over the same immutable train proof."""
        return Evidence(self.base, self.chain, self.pr, self.born, self.excluded)

    def _check_silence(self, ctx, *, status=None, completed=False):
        """The exclusive owner persists progress per immutable tip, across restarts.

        Completed tip evidence counts once; polling pending checks or missing
        artifacts must not hide silence after an interrupted check.
        """
        tip = self.chain[-1][2]
        path = ctx.store.root / "merge-batch" / f"{tip}.json"
        try:
            progress = json.loads(path.read_text())
        except FileNotFoundError:
            progress = {"last_progress": self.born, "ci_status": None}
        progress.setdefault("last_progress_what", "batch built")
        seen = progress.setdefault("seen_jobs", [])
        now = ctx.now().timestamp()

        def moved(what):
            progress["last_progress"], progress["last_progress_what"] = now, what

        if completed:
            moved("tip evidence completed")
        elif status in {"pass", "fail"} and status != progress["ci_status"]:
            moved(f"tip CI {status}")
        if status is not None:
            progress["ci_status"] = status
        # Per-job progress is needed only while the tip is unfinished. Once its
        # result/evidence is available, another network read must not delay it.
        jobs = []
        if not completed and status not in {"pass", "fail"}:
            try:
                jobs = ctx.tracker.head_finished_jobs(tip, branch=_name(self.base, len(self.chain)))
            except TrackerError as exc:
                if exc.waits:
                    raise
                jobs = []  # An unreadable job list is no progress; silence still applies.
        for job in jobs:
            if job.get("id") not in seen:
                seen.append(job.get("id"))
                moved(f"tip CI job '{job.get('name')}' finished ({job.get('conclusion')})")
        ctx.store._write_json(path, progress)
        if now - progress["last_progress"] >= SILENCE_SECONDS:
            last = datetime.fromtimestamp(progress["last_progress"], UTC).strftime("%H:%M UTC")
            raise ValueError("merge batch retired after 60 minutes of silence; "
                             f"last progress: {progress['last_progress_what']}, seen {last}")

    def reports(self, ctx):
        prefix, branch, pr = self.chain[-1][2], _name(self.base, len(self.chain)), self.pr
        if ctx.git.remote_branch_sha(branch) != prefix:
            raise ValueError("candidate ref changed")
        if pr is None:
            self._check_silence(ctx)
            self.pending = True
            raise ci_mode.MissingComparison("prefix PR not registered")
        try:
            row = ctx.tracker.get_pr(pr)
        except TrackerError as exc:
            if not exc.is_not_found:
                raise
            self._check_silence(ctx)
            self.pending = True
            raise ci_mode.MissingComparison("prefix PR not registered") from exc
        if (row.head_sha != prefix or row.head != branch
                or row.base != ctx.config.base_branch or row.state != "open" or row.merged_at):
            raise ValueError("prefix PR changed")
        status = ctx.tracker.pr_checks(pr)
        if status == "fail":
            # The tip must produce its own verdict again after either interruption.
            # Member cancellation coverage separately excludes timeouts.
            if (ctx.tracker.pr_checks_cancelled_only(pr, include_timeouts=True)
                    and ctx.tracker.rerun_cancelled(pr, head=prefix, include_timeouts=True)):
                self._check_silence(ctx)
                self.pending = True
                raise ci_mode.MissingComparison("prefix CI cancelled; re-run requested")
            _ci_result(ctx, prefix, status)
            return status, self.base, prefix, []
        self.pending = status in {"pending", "none"}
        if self.pending:
            self._check_silence(ctx)
            raise ci_mode.MissingComparison("prefix CI pending")
        reports = ctx.tracker.pr_acceptance_reports(prefix)
        if not reports:
            self._check_silence(ctx)
            raise ci_mode.MissingComparison("tip acceptance reports pending")
        measured_base = ci_mode.reported_base(reports)
        if measured_base != self.base:
            raise ValueError("tip measurement base changed")
        _ci_result(ctx, prefix, status)
        self._check_silence(ctx, status=status)
        return status, measured_base, prefix, reports

    def measurement(self, attempt, head, base_sha):
        if base_sha != self.base or not any(n == attempt.n and member == head for n, member, _, _ in self.chain):
            raise ValueError("member head or target changed")
        status, measured_base, measured, reports = self.reports(attempt.ctx)
        if status == "fail":
            raise ValueError("tip CI red")
        return measured_base, measured, reports

    def outcome(self, ctx):
        self.passed = False
        status, origin, sha, reports = self.reports(ctx)
        if status == "fail":
            return "fail"
        excluded = self.judged_exclusions(ctx)
        scope = measurement_scope(ctx.config, ctx.repo_root, excluded)
        lost, _ = ci_mode.read(ctx, reports, origin, sha, excluded, expected_scope=scope,
                               expected_shards=range(ctx.config.ci_shards))
        if any(not ci_mode.scope_covers(item["report"]["scope"], scope) for item in reports):
            raise ci_mode.StaleComparison("tip measurement scope no longer covers the current one")
        self.passed = not lost
        return "pass" if self.passed else "fail"

    def judged_exclusions(self, ctx):
        return list(self.excluded) if self.excluded is not None else _exclusions(ctx, self.base, self.chain)

    def current(self, n, ctx, git, pr, head, base_sha, status):
        if status == "fail":
            member = next((item for item in self.chain if item[0] == n and item[1] == head), None)
            run_ids = cancelled_runs(ctx, n, head, member[3], tip=self.chain[-1][2]) if member else set()
            if not (run_ids and ctx.tracker.pr_checks_cancelled_only(pr, include_timeouts=False,
                                                                    run_ids=run_ids)):
                self.reason = "member CI red"
                return False
        attempt = SimpleNamespace(n=n, ctx=ctx, git=git, target_branch=ctx.config.base_branch,
                                  log=ctx.store.log_path(n))
        _accept_target(ctx, self.base, self.chain, base_sha, reason="member head or target changed")
        # Each final read binds trust to the current default branch. A prior
        # read's trusted SHA may have moved along with an accepted neutral target.
        accepted = ci_mode.require_comparison(attempt, pr, head, base_sha=self.base, status=status,
                                              wait=False, source=self).follow(base_sha)
        if not accepted.current(n, ctx, git, pr, head, base_sha, accepted.status):
            raise ci_mode.StaleComparison("tip comparison changed")
        return True


class MemberEvidence(Evidence):
    """PM r1039: a one-member remainder proved by its own PR CI on the same main.

    After a final check names the culprit, the rest needs no new batch CI when
    its PR CI already measured it on the unchanged target. The train landing
    path still applies every final predicate before the leased target update.
    """

    def __init__(self, base, chain, pr, born, excluded=None, *, member_pr):
        super().__init__(base, chain, pr, born, excluded)
        self.member_pr = member_pr

    def fresh(self):
        return MemberEvidence(self.base, self.chain, self.pr, self.born, self.excluded, member_pr=self.member_pr)

    def _check_silence(self, ctx, *, status=None, completed=False):
        """No tip CI runs for an unpublished direct landing; its member PR CI already finished."""

    def reports(self, ctx):
        head = self.chain[-1][1]
        row = ctx.tracker.get_pr(self.member_pr)
        if (row.head_sha != head or row.base != ctx.config.base_branch
                or row.state != "open" or row.merged_at):
            raise ValueError("member PR changed")
        status = ctx.tracker.pr_checks(self.member_pr)
        if status != "pass":
            raise ValueError(f"member PR CI {status}")
        reports = ctx.tracker.pr_acceptance_reports(head)
        if ci_mode.reported_base(reports) != self.base:
            raise ValueError("member measurement base changed")
        if _tree(ctx.git, self.chain[-1][2]) != _tree(ctx.git, head):
            raise ValueError("member PR CI did not measure the landing tree")
        self._check_silence(ctx, status=status)
        return status, self.base, head, reports


def _join(ctx, report, base, before, place, job):
    """Repair target conflicts; skip other failures unless the head is included."""
    try:
        head = job["head"]
        if ci_mode.pr_changes_workflows(ctx.git, base, head):
            raise ValueError("acceptance workflow differs from target")
        if not ctx.git.is_ancestor(head, before):
            try:
                tree = ctx.git._call(["merge-tree", "--write-tree", before, head]).splitlines()[0]
            except GitError as exc:
                cause = exc.__cause__
                if not (isinstance(cause, subprocess.CalledProcessError) and cause.returncode == 1):
                    raise
                if before != base:
                    try:
                        ctx.git._call(["merge-tree", "--write-tree", base, head])
                    except GitError as target_error:
                        cause = target_error.__cause__
                        if not (isinstance(cause, subprocess.CalledProcessError) and cause.returncode == 1):
                            raise
                    else:
                        raise exc  # This head conflicts only with an earlier member.
                from foragent import merge_owner as owner

                repaired = owner._try_admission(ctx, place, job, lambda: owner._repair(
                    ctx, place, job, f"Base refresh failed (base-changed); repair the PR on {base}."))
                if repaired and job["phase"] == "repair-requested":
                    report.actions.append(f"#{place.n} sent back to lane: conflicts with {base[:8]}")
                    return None
                raise exc
            message = f"Merge member #{place.n}\n\nForagent-Member: #{place.n} {head} {place.started_at}\n"
            commit = ctx.git._call(["-c", "user.name=foragent", "-c", "user.email=foragent@localhost",
                                    "commit-tree", tree, "-p", before, "-p", head, "-m", message]).strip()
            return place.n, head, commit, place.started_at
    except Exception as exc:
        if isinstance(exc, TrackerError) and exc.waits:
            raise
        report.actions.append(f"#{place.n} merge train excluded: {str(exc) or type(exc).__name__}")
        return None
    # Its code remains in the prefix, so excluding it would skip its checks.
    raise ValueError("member already reachable")


def _failed_candidates(ctx, retired, candidates, base):
    """Replay failed sets from facts; a landed half leaves one whole remainder."""
    def key(member):
        return member["n"], member["head"], member["started_at"]

    def recorded_base(row):
        return row.get("base") or ctx.git._call([
            "rev-parse", f"{row['candidate']}~{len(row['members'])}"]).strip()

    waiting = {(p.n, j["head"], p.started_at): (p, j, pr) for p, j, pr in candidates
               if pr.head_sha == j["head"]}
    passed, stopped = [], []
    for row in reversed(retired):
        if row.get("passed") or row.get("reason") == "train merged":
            passed.append(row)
        elif row.get("reason") != "tip CI red" or len(row["members"]) == 1:
            # A red single-member retry is spent and already explains its set's red,
            # so the rest of that set rejoins normal batching instead of a solo train.
            stopped.append({key(m) for m in row["members"]})
        if row.get("reason") != "tip CI red":
            continue
        members = row["members"]
        if not any(key(m) in waiting for m in members):
            continue
        landed = {key(m) for m in members if ctx.git.is_ancestor(m["head"], base)}
        remaining = [waiting[key(m)] for m in members if key(m) in waiting and key(m) not in landed]
        if not remaining:
            continue
        identities = {key(m) for m in members}
        # A later non-green retirement of a retry resolves its containing failed sets.
        if any(attempt and attempt <= identities for attempt in stopped):
            continue
        if any(identities <= {key(m) for m in success["members"]}
               and recorded_base(row) != recorded_base(success)
               and ctx.git.is_ancestor(recorded_base(row), recorded_base(success))
               and ctx.git.is_ancestor(recorded_base(success), base) for success in passed):
            continue
        return remaining if landed else remaining[:(len(remaining) + 1) // 2]
    return None


def _culprit_remainder(ctx, retired, candidates, base):
    """The members left after a failed final check named the culprit (PM r1039).

    Only the latest retirement counts: once the remainder's own train ends, its
    retirement replaces this one. The old train's result covered the culprit too,
    so it is never reused for the remainder.
    """
    if not retired or not retired[-1].get("culprit"):
        return None
    row = retired[-1]
    spent = {(m["n"], m["started_at"]) for m in row["spent"]}
    waiting = {(p.n, j["head"], p.started_at): (p, j, pr) for p, j, pr in candidates
               if pr.head_sha == j["head"]}
    remaining = [waiting.get((m["n"], m["head"], m["started_at"])) for m in row["members"]
                 if (m["n"], m["started_at"]) not in spent and not ctx.git.is_ancestor(m["head"], base)]
    if not remaining or None in remaining:
        return None  # A member left the single path; the rest rejoins normal batching.
    return remaining


def _verified_alone(ctx, remainder, base):
    """A one-member remainder whose own PR CI passed on this main.

    That CI measured the very tree that merging it onto this target lands, so it
    needs no new batch CI.
    """
    if len(remainder) != 1:
        return None
    (p, j, pr), = remainder
    if (ctx.tracker.pr_checks(pr.number) == "pass"
            and ci_mode.reported_base(ctx.tracker.pr_acceptance_reports(j["head"])) == base
            and _head_tree_lands(ctx.git, base, j["head"])):
        return (p, j, pr)
    return None


def prepare(ctx, report, candidates):
    """None permits single admissions; False ends a reserved or retired train's tick."""
    base, chain, proof, direct = None, [], None, None
    try:
        retired = retirements(ctx)
        retired_ids = {row.get("identity") for row in retired}
        recovered, damaged = _discover(ctx, report)
        active = []
        for recovered_base, recovered_chain in recovered:
            if any(row.get("identity") == _identity(recovered_base, recovered_chain)
                   or "identity" not in row and row["candidate"] == recovered_chain[-1][2] for row in retired):
                _close(ctx, report, recovered_base, recovered_chain, "candidate already retired")
            else:
                active.append((recovered_base, recovered_chain))
        recovered = active
        if damaged or len(recovered) > 1:
            for base, chain in recovered:
                _close(ctx, report, base, chain, "published prefix ledgers changed")
            _retire_damaged(ctx, damaged)
            return False
        if recovered:
            base, chain = recovered[0]
        elif orch_train_pause.active(ctx.store, ctx.now()) is not None:
            return None  # `fa orch pause-trains`: the running train finishes above; no new one is built.
        else:
            spent = {(m["n"], m["started_at"]) for row in retired for m in row.get("spent", [])}
            ctx.git.fetch("origin", ctx.config.base_branch)
            base = ctx.git.ref_sha(f"origin/{ctx.config.base_branch}")
            eligible = [(p, j, pr) for p, j, pr in candidates
                        if (p.n, p.started_at) not in spent and j["phase"] == "waiting"
                        and j["target"] == ctx.config.base_branch]
            remainder = _culprit_remainder(ctx, retired, eligible, base)
            verified = remainder and _verified_alone(ctx, remainder, base)
            direct = [verified] if verified else None
            # An unverified remainder is verified on its own, without other queued PRs.
            retry = remainder if remainder and not verified else _failed_candidates(ctx, retired, eligible, base)
            # A failed set reserves only a new, composable retry. Otherwise
            # normal batching gets the same pass, with all members still eligible.
            # One composable admission is a batch of one (Jin 2026-11-05): it uses
            # the same build, CI wait, landing and retirement path as a larger one.
            for selection, minimum in ((direct, 1), (retry, 1), (eligible, 1)):
                if selection is None:
                    continue
                chain, before = [], base
                for p, j, _ in selection:
                    if j["phase"] != "waiting":
                        continue  # A previous selection may have returned this admission to its lane.
                    member = _join(ctx, report, base, before, p, j)
                    if ctx.store.github_wait_until(ctx.now()):
                        return False
                    if member is None:
                        continue
                    chain.append(member)
                    before = member[2]
                    if len(chain) == ctx.config.merge_batch_size:
                        if _identity(base, chain) not in retired_ids:
                            break
                        chain, before = [], base
                if len(chain) >= minimum and _identity(base, chain) not in retired_ids:
                    direct = direct if selection is direct else None
                    break
            else:
                return None
            # Only the tip is published. Bind its CI scope to the members that
            # actually composed, without repeating their merge-tree checks.
            excluded = _exclusions(ctx, base, chain, own=not direct)
            n, head, tip, started = chain[-1]
            before = chain[-2][2] if len(chain) > 1 else base
            message = ctx.git._call(["show", "-s", "--format=%B", tip]).rstrip()
            message += f"\n{EXCLUSIONS_TRAILER}: {json.dumps(excluded, separators=(',', ':'))}"
            tip = ctx.git._call(["-c", "user.name=foragent", "-c", "user.email=foragent@localhost",
                                 "commit-tree", _tree(ctx.git, tip), "-p", before, "-p", head, "-m", message]).strip()
            chain[-1] = (n, head, tip, started)
        used = _landed(ctx, base, chain)
        if used:
            _close(ctx, report, base, chain, "member heads already reachable from target", used=used)
            return None
        _accept_target(ctx, base, chain, ctx.git.ref_sha(f"origin/{ctx.config.base_branch}"))
        born = int(ctx.git._call(["show", "-s", "--format=%ct", chain[0][2]]))
        available = {(p.n, p.started_at) for p, _, _ in candidates}
        if any((n, started) not in available for n, _, _, started in chain):
            raise ValueError("member left the single path")
        excluded = _recorded_exclusions(ctx.git, chain[-1][2])
        if direct:
            # PM r1039: the member's own PR CI is the proof, so no batch CI wait starts.
            proof = MemberEvidence(base, chain, None, born, excluded, member_pr=direct[0][2].number)
        else:
            members = {(p.n, p.started_at): pr for p, _, pr in candidates}
            proof = Evidence(base, chain, _publish(ctx, report, base, chain, len(chain), members), born, excluded)
            from foragent import merge_owner as owner

            identities = {(n, started) for n, _, _, started in chain}
            for place, job, _ in candidates:
                if (place.n, place.started_at) in identities:
                    owner._set_phase(ctx, place, job, "batch-ci")
        try:
            status = proof.outcome(ctx)
        except ci_mode.MissingComparison as exc:
            ctx.merge_owner_wait_cause = f"waiting behind merge train CI: {exc}"
            return False  # Keep the reservation until completion or the silence deadline.
        if status == "fail":
            raise ValueError("tip CI red")
        return proof
    except Exception as exc:
        if isinstance(exc, TrackerError) and exc.waits:
            raise
        if chain:
            _close(ctx, report, base, chain, str(exc) or type(exc).__name__, tip_pr=proof.pr if proof else None,
                   passed=proof.passed if proof else False)
            return False
        report.actions.append(f"merge train unavailable: {exc}")
        return None


def land(ctx, report, candidates, proof):
    """Reuse every member's final check before the sole target ref update."""
    from foragent import merge_owner as owner

    members = {(p.n, p.started_at): (p, j, pr) for p, j, pr in candidates}
    failed, checking, culprit, deferred_phase = None, None, False, None
    reason = "train merged"
    try:
        ctx.merge_owner_started_at = ctx.now()
        for n, _, _, started in proof.chain:
            place, job, _ = members[(n, started)]
            owner._set_phase(ctx, place, job, "merge-owner-checks")
        with ExitStack() as stack:
            prepared = []
            for member in proof.chain:
                n, head, _, started = member
                place, job, pr = members[(n, started)]
                if job["head"] != head:
                    raise ValueError("member head changed")
                git = stack.enter_context(owner._member_worktree(ctx, job))
                evidence = proof.fresh()
                prepared.append((place, job, pr, git, evidence))
            tip = stack.enter_context(owner._member_worktree(ctx, {
                "head": proof.chain[-1][2], "target": ctx.config.base_branch,
            }))
            files = sorted({path for _, job, *_ in prepared
                            for path in parse_body(job["approval"]["body"]).files})
            tip_sha = proof.chain[-1][2]
            members_named = " ".join(f"#{n}" for n, *_ in proof.chain)

            def recheck():
                """Use the final predicates between checks as well as before publication."""
                nonlocal checking, failed, reason
                checking = None  # Batch-wide predicates have no member owner.
                with ctx.reads.fresh():
                    if proof.outcome(ctx) != "pass":
                        raise ValueError("tip CI red")
                    ctx.git.fetch("origin", ctx.config.base_branch)
                    target_base = ctx.git.ref_sha(f"origin/{ctx.config.base_branch}")
                    _accept_target(ctx, proof.base, proof.chain, target_base, reason="member head or target changed")
                    owner.run_publish._check_publication(tip, ctx.config.base_branch, target_base)
                    for member, (place, job, pr, git, evidence) in zip(proof.chain, prepared):
                        checking = member
                        if not owner._ready(ctx, place, job, pr, git, target_base, evidence):
                            failed = member
                            reason = evidence.reason or f"single path final check {job['phase']}"
                            raise ValueError(reason)
                        approval = load_record(Approval, job["approval"])
                        if changed := acceptance_mismatches(tip, approval, proof.chain[-1][2]):
                            raise ValueError(f"member #{place.n}: approved acceptance files changed on train tip: "
                                             + ", ".join(changed))
                    checking = None
                    _validate_trees(ctx.git, proof.base, proof.chain)
                    return target_base

            places = [place for place, *_ in prepared]
            with (owner.timed_checks(ctx, f"{_name(proof.base, len(proof.chain))} ({members_named})", tip_sha),
                  owner.owner_activity(ctx, places, "checking", "merge-check")):
                # D124: skip exactly what CI passed on the tip these checks run on.
                skips = ci_local.PassSkips.read(ctx, tip_sha, trusted_tip=True)
                for member, (place, job, _, _, _) in zip(proof.chain, prepared):
                    checking = member
                    link = f"https://github.com/{ctx.repo}/pull/{job['pr']}"

                    def verify(head, place=place, job=job, link=link):
                        return owner._verify_review_head(place.n, ctx, tip, parse_body(job["approval"]["body"]), head,
                                                         link, skips=skips)

                    guarded = harness_guard.review_guard(ctx, place.n, tip, f"origin/{ctx.config.base_branch}",
                                                         files, link, verify)
                    with owner._track_checks(ctx, place, job):
                        if stopped := guarded(proof.chain[-1][2]):
                            raise AttemptFailure(f"Train tip Done when: {stopped}", ctx.config.base_branch)
                    proof._check_silence(ctx, completed=True)
                    recheck()
                checking = None
                place, job, *_ = prepared[0]
                with owner._track_checks(ctx, place, job):
                    ci_mode.review_checks(place.n, ctx, tip, ctx.config.base_branch, skips=skips,
                                          files=files, excluded=proof.judged_exclusions(ctx))
                proof._check_silence(ctx, completed=True)
            untracked = tip._call([
                "ls-files", "--others", "--exclude-standard", "-z", "--", ".",
                *(f":(exclude,literal){path}" for path in sorted(tip.artifact_paths())),
            ])
            if (tip.head_sha() != proof.chain[-1][2]
                    or tip._call(["status", "--porcelain=v1", "--untracked-files=no"]) or untracked):
                raise ValueError("Train tip machine checks modified tracked files, created untracked files, or changed HEAD")
            with ctx.store.merge_lock(), main_write_lock(ctx.git), ctx.reads.fresh():
                target_base = recheck()
                landing = proof.chain[-1][2]
                if target_base != proof.base:
                    tree = ctx.git._call(["merge-tree", "--write-tree", target_base, landing]).splitlines()[0]
                    # Keep the measured tip on the first-parent chain so CI
                    # can still read its fixed exclusions trailer.
                    landing = ctx.git._call(["-c", "user.name=foragent", "-c", "user.email=foragent@localhost",
                                             "commit-tree", tree, "-p", landing, "-p", target_base,
                                             "-m", "Merge verified batch onto result-neutral target"]).strip()
                with orch_stop.merging(ctx, prepared[0][0].n):
                    for place, job, _, _, _ in prepared:
                        job.update(phase="merging", merge_base=target_base)
                        owner._save(ctx, place, job)
                    target = f"refs/heads/{ctx.config.base_branch}"
                    with owner.owner_activity(ctx, places, "merging", "initial"):
                        ctx.git._call(["push", f"--force-with-lease={target}:{target_base}",
                                       "origin", f"{landing}:{target}"])
        if failed is None:
            for place, job, _, _, _ in prepared:
                with ctx.reads.fresh():
                    owner._try_admission(ctx, place, job, lambda: owner._reconcile(ctx, place, job))
    except ci_mode.MissingComparison as exc:
        ctx.merge_owner_wait_cause = f"waiting for merge train CI: {exc}"
        deferred_phase = "batch-ci"
        return  # CI registration/status can change during the unlocked checks too.
    except Exception as exc:
        if isinstance(exc, ExecutorStopped) and exc.outcome == "stopped":
            ctx.merge_owner_wait_cause = str(exc)
            deferred_phase = "merge-owner-queue"
            return  # Keep the published train and its admissions for the release.
        if isinstance(exc, TrackerError) and exc.waits:
            deferred_phase = "merge-owner-queue"
            raise
        reason = str(exc) or type(exc).__name__
        if checking:
            place, job, _ = members[(checking[0], checking[3])]

            def rejected():
                raise exc

            owner._try_admission(ctx, place, job, rejected)
            if isinstance(exc, (AttemptFailure, ExecutorStopped, LocalComparisonUnavailable)):
                failed = checking
                # Only a failed check is a verdict on the member's code. A member that
                # left the single path (held, closed, superseded) or could not be checked
                # names no culprit, so the rest of its batch was never cleared by elimination.
                culprit = isinstance(exc, AttemptFailure)
    finally:
        if deferred_phase:
            for n, _, _, started in proof.chain:
                place, job, _ = members[(n, started)]
                if job["phase"] in {"waiting", "publishing", "merging"}:
                    owner._set_phase(ctx, place, job, deferred_phase)
    if failed:
        reason = f"member #{failed[0]}: {reason}"
    _close(ctx, report, proof.base, proof.chain, reason, spent=[failed] if failed else [], tip_pr=proof.pr,
           passed=proof.passed, culprit=culprit)
