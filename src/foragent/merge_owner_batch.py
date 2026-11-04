"""Test a merge train tip, then publish the whole train with one leased update."""

from contextlib import ExitStack
from hashlib import sha256
from types import SimpleNamespace
import json

from foragent import ci_mode, harness_guard, orch_stop
from foragent.ci_workflow import EXCLUSIONS_TRAILER, measurement_scope
from foragent.ci_local import LocalComparisonUnavailable
from foragent.git import main_write_lock
from foragent.issue import parse_body
from foragent.records import load_record
from foragent.run_attempt import AttemptFailure, ExecutorStopped
from foragent.run_baseline import pending_acceptance_exclusions
from foragent.run_checks_approved import acceptance_mismatches
from foragent.run_publish_ci import _CIWait
from foragent.state import Approval
from foragent.tracker import TrackerError

PREFIX = "foragent/batch-merge-"
SPENT = "refs/foragent/batch-spent/"
DEADLINE_SECONDS = 3 * _CIWait(0).deadline


def _name(base, k):
    return f"{PREFIX}{base[:8]}-{k}"


def _tree(git, commit):
    return git._call(["show", "-s", "--format=%T", commit]).strip()


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


def _exclusions(ctx, base, chain):
    return pending_acceptance_exclusions(
        ctx, (base, *(head for _, head, _, _ in chain)), ctx.store.log_path(chain[0][0]),
        own=[n for n, *_ in chain])


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
    print(f"merge batch {identity[:12]} candidate {candidate[:12]} retired: {reason}", flush=True)
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


def _publish(ctx, base, chain, k):
    branch, prefix = _name(base, k), chain[k - 1][2]
    remote = ctx.git.remote_branch_sha(branch)
    if remote is None:
        ctx.git._call(["push", f"--force-with-lease=refs/heads/{branch}:",
                       "origin", f"{prefix}:refs/heads/{branch}"])
    elif remote != prefix:
        raise ValueError("candidate ref changed")
    return _prefix_pr(ctx, base, k)


def _close(ctx, report, base, chain, reason, used=None, spent=(), tip_pr=None, passed=False):
    from foragent.merge_owner import close_for_repair

    minutes = 0
    try:
        minutes = sum(item["report"]["duration_seconds"]
                      for item in ctx.tracker.pr_acceptance_reports(chain[-1][2])) / 60
    except Exception as exc:
        if isinstance(exc, TrackerError) and exc.waits:
            raise
        pass  # Missing measurements are unknown; cleanup must still proceed.
    members = [dict(n=n, head=head, started_at=started) for n, head, _, started in chain]
    if reason == "tip CI red" and len(chain) == 1:
        spent = chain
    # Persist retirement before deleting the tip, so cleanup can resume after a crash.
    refs = {_name(base, len(chain)): chain[-1][2]} if chain[-1][2] != base else {}
    reason = _record_retirement(ctx, _identity(base, chain), chain[-1][2], reason=reason,
                               base=base, passed=passed, members=members, refs=refs,
                               spent=[dict(n=n, head=h, started_at=s) for n, h, _, s in spent])
    if used is None:
        used = _landed(ctx, base, chain)
    if not any(row["kind"] == "merge-batch" and row["data"].get("members") == members
               for row in ctx.store.flow_records()):
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
        _close(ctx, report, base, chain, "merge batching disabled")
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

    def __init__(self, base, chain, pr, born):
        self.base, self.chain, self.pr, self.born = base, chain, pr, born
        self.reason = None
        self.pending = False
        self.accepted = None
        self.passed = False

    def reports(self, ctx):
        prefix, branch, pr = self.chain[-1][2], _name(self.base, len(self.chain)), self.pr
        if ctx.now().timestamp() - self.born >= DEADLINE_SECONDS:
            raise ValueError("three CI rounds elapsed")
        if ctx.git.remote_branch_sha(branch) != prefix:
            raise ValueError("candidate ref changed")
        if pr is None:
            self.pending = True
            raise ci_mode.MissingComparison("prefix PR not registered")
        try:
            row = ctx.tracker.get_pr(pr)
        except TrackerError as exc:
            if not exc.is_not_found:
                raise
            self.pending = True
            raise ci_mode.MissingComparison("prefix PR not registered") from exc
        if (row.head_sha != prefix or row.head != branch
                or row.base != ctx.config.base_branch or row.state != "open" or row.merged_at):
            raise ValueError("prefix PR changed")
        status = ctx.tracker.pr_checks(pr)
        if status == "fail":
            return status, self.base, prefix, []
        self.pending = status in {"pending", "none"}
        if self.pending:
            raise ci_mode.MissingComparison("prefix CI pending")
        reports = ctx.tracker.pr_acceptance_reports(prefix)
        if not reports:
            raise ci_mode.MissingComparison("tip acceptance reports pending")
        measured_base = ci_mode.reported_base(reports)
        if measured_base != self.base:
            raise ValueError("tip measurement base changed")
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
        excluded = _exclusions(ctx, self.base, self.chain)
        scope = measurement_scope(ctx.config, ctx.repo_root, excluded)
        lost, _ = ci_mode.comparison(reports, ctx.repo, origin, sha, excluded, expected_scope=scope,
                                    expected_shards=range(ctx.config.ci_shards))
        if any(not ci_mode.scope_covers(item["report"]["scope"], scope) for item in reports):
            raise ci_mode.StaleComparison("tip measurement scope no longer covers the current one")
        self.passed = not lost
        return "pass" if self.passed else "fail"

    def current(self, n, ctx, git, pr, head, base_sha, status):
        if status == "fail":
            self.reason = "member CI red"
            return False
        attempt = SimpleNamespace(n=n, ctx=ctx, git=git, target_branch=ctx.config.base_branch,
                                  log=ctx.store.log_path(n))
        if self.accepted is None:
            self.accepted = ci_mode.require_comparison(attempt, pr, head, base_sha=base_sha, status=status,
                                                       wait=False, source=self)
        else:
            self.measurement(attempt, head, base_sha)
        if not self.accepted.current(n, ctx, git, pr, head, base_sha, self.accepted.status):
            raise ci_mode.StaleComparison("tip comparison changed")
        return True


def _join(ctx, report, base, before, place, job):
    """Skip composition failures; an already included head aborts the train."""
    try:
        head = job["head"]
        if ci_mode.pr_changes_workflows(ctx.git, base, head):
            raise ValueError("acceptance workflow differs from target")
        if not ctx.git.is_ancestor(head, before):
            tree = ctx.git._call(["merge-tree", "--write-tree", before, head]).splitlines()[0]
            message = f"Merge member #{place.n}\n\nForagent-Member: #{place.n} {head} {place.started_at}\n"
            commit = ctx.git._call(["-c", "user.name=foragent", "-c", "user.email=foragent@localhost",
                                    "commit-tree", tree, "-p", before, "-p", head, "-m", message]).strip()
            return place.n, head, commit, place.started_at
    except Exception as exc:
        if isinstance(exc, TrackerError) and exc.waits:
            raise
        report.actions.append(f"#{place.n} merge batch excluded: {str(exc) or type(exc).__name__}")
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
        elif row.get("reason") != "tip CI red":
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


def prepare(ctx, report, candidates):
    """None permits single admissions; False ends a reserved or retired train's tick."""
    base, chain, proof = None, [], None
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
        else:
            spent = {(m["n"], m["started_at"]) for row in retired for m in row.get("spent", [])}
            ctx.git.fetch("origin", ctx.config.base_branch)
            base = ctx.git.ref_sha(f"origin/{ctx.config.base_branch}")
            eligible = [(p, j, pr) for p, j, pr in candidates
                        if (p.n, p.started_at) not in spent and j["phase"] == "waiting"
                        and j["target"] == ctx.config.base_branch]
            retry = _failed_candidates(ctx, retired, eligible, base)
            # A failed set reserves only a new, composable retry. Otherwise
            # normal batching gets the same pass, with all members still eligible.
            for selection, minimum in ((retry, 1), (eligible, 2)):
                if selection is None:
                    continue
                chain, before = [], base
                for p, j, _ in selection:
                    member = _join(ctx, report, base, before, p, j)
                    if member is None:
                        continue
                    chain.append(member)
                    before = member[2]
                    if len(chain) == ctx.config.merge_batch_size:
                        if _identity(base, chain) not in retired_ids:
                            break
                        chain, before = [], base
                if len(chain) >= minimum and _identity(base, chain) not in retired_ids:
                    break
            else:
                return None
            # Only the tip is published. Bind its CI scope to the members that
            # actually composed, without repeating their merge-tree checks.
            excluded = _exclusions(ctx, base, chain)
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
        if ctx.git.ref_sha(f"origin/{ctx.config.base_branch}") != base:
            raise ValueError("target changed")
        born = int(ctx.git._call(["show", "-s", "--format=%ct", chain[0][2]]))
        available = {(p.n, p.started_at) for p, _, _ in candidates}
        if any((n, started) not in available for n, _, _, started in chain):
            raise ValueError("member left the single path")
        proof = Evidence(base, chain, _publish(ctx, base, chain, len(chain)), born)
        try:
            status = proof.outcome(ctx)
        except ci_mode.MissingComparison:
            return False  # The published train retains its reservation until CI or its deadline.
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
        report.actions.append(f"merge batch unavailable: {exc}")
        return None


def land(ctx, report, candidates, proof):
    """Reuse every member's final check before the sole target ref update."""
    from foragent import merge_owner as owner

    members = {(p.n, p.started_at): (p, j, pr) for p, j, pr in candidates}
    failed, checking = None, None
    reason = "train merged"
    try:
        ctx.merge_owner_started_at = ctx.now()
        with ExitStack() as stack:
            prepared = []
            for member in proof.chain:
                n, head, _, started = member
                place, job, pr = members[(n, started)]
                if job["head"] != head:
                    raise ValueError("member head changed")
                git = stack.enter_context(owner._member_worktree(ctx, job))
                evidence = Evidence(proof.base, proof.chain, proof.pr, proof.born)
                prepared.append((place, job, pr, git, evidence))
            tip = stack.enter_context(owner._member_worktree(ctx, {
                "head": proof.chain[-1][2], "target": ctx.config.base_branch,
            }))
            files = sorted({path for _, job, *_ in prepared
                            for path in parse_body(job["approval"]["body"]).files})
            for member, (place, job, _, _, _) in zip(proof.chain, prepared):
                checking = member
                link = f"https://github.com/{ctx.repo}/pull/{job['pr']}"

                def verify(head):
                    return owner._verify_review_head(place.n, ctx, tip, parse_body(job["approval"]["body"]), head, link)

                guarded = harness_guard.review_guard(ctx, place.n, tip, f"origin/{ctx.config.base_branch}",
                                                     files, link, verify)
                with owner._track_checks(ctx, place, job):
                    if stopped := guarded(proof.chain[-1][2]):
                        raise AttemptFailure(f"Train tip Done when: {stopped}", ctx.config.base_branch)
            checking = None
            place, job, *_ = prepared[0]
            with owner._track_checks(ctx, place, job):
                ci_mode.review_checks(place.n, ctx, tip, ctx.config.base_branch,
                                      files=files, excluded=_exclusions(ctx, proof.base, proof.chain))
            untracked = tip._call([
                "ls-files", "--others", "--exclude-standard", "-z", "--", ".",
                *(f":(exclude,literal){path}" for path in sorted(tip.artifact_paths())),
            ])
            if (tip.head_sha() != proof.chain[-1][2]
                    or tip._call(["status", "--porcelain=v1", "--untracked-files=no"]) or untracked):
                raise ValueError("Train tip machine checks modified tracked files, created untracked files, or changed HEAD")
            with ctx.store.merge_lock(), main_write_lock(ctx.git), ctx.reads.fresh():
                if proof.outcome(ctx) != "pass":
                    raise ValueError("tip CI red")
                owner.run_publish._check_publication(tip, ctx.config.base_branch, proof.base)
                for member, (place, job, pr, git, evidence) in zip(proof.chain, prepared):
                    checking = member
                    if not owner._ready(ctx, place, job, pr, git, proof.base, evidence):
                        failed = member
                        reason = evidence.reason or f"single path final check {job['phase']}"
                        break
                    approval = load_record(Approval, job["approval"])
                    if changed := acceptance_mismatches(tip, approval, proof.chain[-1][2]):
                        raise ValueError(f"member #{place.n}: approved acceptance files changed on train tip: "
                                         + ", ".join(changed))
                else:
                    checking = None
                    _validate_trees(ctx.git, proof.base, proof.chain)
                    with orch_stop.merging(ctx, prepared[0][0].n):
                        for place, job, _, _, _ in prepared:
                            job.update(phase="merging", merge_base=proof.base)
                            owner._save(ctx, place, job)
                        target = f"refs/heads/{ctx.config.base_branch}"
                        ctx.git._call(["push", f"--force-with-lease={target}:{proof.base}",
                                       "origin", f"{proof.chain[-1][2]}:{target}"])
        if failed is None:
            for place, job, _, _, _ in prepared:
                with ctx.reads.fresh():
                    owner._try_admission(ctx, place, job, lambda: owner._reconcile(ctx, place, job))
    except ci_mode.MissingComparison:
        return  # CI registration/status can change during the unlocked checks too.
    except Exception as exc:
        if isinstance(exc, ExecutorStopped) and exc.outcome == "stopped":
            return  # Keep the published train and its admissions for the release.
        if isinstance(exc, TrackerError) and exc.waits:
            raise
        reason = str(exc) or type(exc).__name__
        if checking:
            place, job, _ = members[(checking[0], checking[3])]

            def rejected():
                raise exc

            owner._try_admission(ctx, place, job, rejected)
            if isinstance(exc, (AttemptFailure, ExecutorStopped, LocalComparisonUnavailable)):
                failed = checking
    if failed:
        reason = f"member #{failed[0]}: {reason}"
    _close(ctx, report, proof.base, proof.chain, reason, spent=[failed] if failed else [], tip_pr=proof.pr,
           passed=proof.passed)
