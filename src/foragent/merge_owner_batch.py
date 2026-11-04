"""Disposable prefix CI evidence. Only merge_owner's single path can merge a PR."""

from hashlib import sha256
from types import SimpleNamespace
import json

from foragent import ci_mode
from foragent.ci_workflow import EXCLUSIONS_TRAILER
from foragent.run_baseline import _baseline_exclusions
from foragent.run_publish_ci import _CIWait
from foragent.tracker import TrackerError

PREFIX = "foragent/batch-merge-"
SPENT = "refs/foragent/batch-spent/"
DEADLINE_SECONDS = 3 * _CIWait(0).deadline


def _name(base, k):
    return f"{PREFIX}{base[:8]}-{k}"


def _tree(git, commit):
    return git._call(["show", "-s", "--format=%T", commit]).strip()


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
    return commit, chain


def _close(ctx, report, base, chain, reason, used=None):
    from foragent.merge_owner import close_for_repair

    minutes = 0
    for _, _, prefix, _ in chain:
        try:
            minutes += sum(item["report"]["duration_seconds"]
                           for item in ctx.tracker.pr_acceptance_reports(prefix)) / 60
        except Exception as exc:
            if isinstance(exc, TrackerError) and exc.waits:
                raise
            pass  # Missing measurements are unknown; cleanup must still proceed.
    rows = ctx.store.flow_records()
    members = [dict(n=n, head=head, started_at=started) for n, head, _, started in chain]
    # Publish every spent generation together, before deleting any prefix. A crash
    # before update-ref leaves no marker; a crash after it retains the whole ledger.
    ledger = json.dumps(dict(candidate=chain[-1][2], members=members), separators=(",", ":"))
    retired = ctx.git._call(["-c", "user.name=foragent", "-c", "user.email=foragent@localhost",
                            "commit-tree", _tree(ctx.git, chain[-1][2]), "-p", chain[-1][2],
                            "-m", f"Retire merge batch\n\nForagent-Batch: {ledger}"]).strip()
    ctx.git._call(["update-ref", f"{SPENT}{sha256(ledger.encode()).hexdigest()}", retired])
    ctx.git.fetch("origin", ctx.config.base_branch)
    if used is None:
        actual = ctx.git._call(["log", "--first-parent", "--reverse", "--format=%T",
                                f"{base}..origin/{ctx.config.base_branch}"]).splitlines()
        used = 0
        for tree, (_, _, prefix, _) in zip(actual, chain):
            if tree != _tree(ctx.git, prefix):
                break
            used += 1
    if not any(row["kind"] == "merge-batch" and row["data"].get("members") == members for row in rows):
        ctx.store.record_flow("merge-batch", members=members, candidate=chain[-1][2],
                              result="used" if used else "unused", used_members=used,
                              reason=reason, ci_minutes=minutes)
    for k, (_, _, prefix, _) in enumerate(chain, 1):
        branch = _name(base, k)
        try:
            if prefix == base or ctx.git.remote_branch_sha(branch) != prefix:
                continue
            pr = ctx.tracker.find_pr(branch)
            if pr is not None:
                close_for_repair(ctx, None, pr, prefix, ctx.config.base_branch, branch=branch)
            ctx.tracker.cancel_workflow_runs(prefix, branch=branch)
            ctx.git._call(["push", f"--force-with-lease=refs/heads/{branch}:{prefix}",
                           "origin", f":refs/heads/{branch}"])
        except Exception as exc:
            if isinstance(exc, TrackerError) and exc.waits:
                raise
            report.actions.append(f"{branch} cleanup deferred: {exc}")
            break  # Retain the tip ledger until all earlier refs are gone.


def _discover(ctx, report):
    """Recover each intact ledger independently of any altered prefix ref."""
    refs = ctx.git._call(["ls-remote", "--heads", "origin", f"refs/heads/{PREFIX}*"])
    recovered, damaged = [], False
    for candidate, ref in (line.split() for line in refs.splitlines()):
        branch = ref.removeprefix("refs/heads/")
        try:
            ctx.git.fetch("origin", branch)
            base, chain = _decode(ctx.git, branch, candidate)
        except Exception as exc:
            if isinstance(exc, TrackerError) and exc.waits:
                raise
            damaged = True
            report.actions.append(f"{branch} ledger unavailable: {exc}")
            continue
        # A longer intact ledger already accounts for its shorter prefixes.
        if any(b == base and c[:len(chain)] == chain for b, c in recovered):
            continue
        recovered = [(b, c) for b, c in recovered if b != base or chain[:len(c)] != c]
        recovered.append((base, chain))
    return recovered, damaged


def retire(ctx, report):
    """Retirement remains available when new batching and CI reuse are disabled."""
    recovered, _ = _discover(ctx, report)
    for base, chain in recovered:
        _close(ctx, report, base, chain, "merge batching disabled")


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

    def __init__(self, base, chain, index, prs, born):
        self.base, self.chain, self.index = base, chain, index
        self.born = born
        self.reason = None
        self.pending = False
        self.pr = prs.get(index)
        self.accepted = None

    def measurement(self, attempt, head, base_sha):
        ctx, git = attempt.ctx, attempt.git
        prefix = self.chain[self.index][2]
        previous = self.chain[self.index - 1][2] if self.index else self.base
        if _tree(git, head) != _tree(git, prefix) or _tree(git, base_sha) != _tree(git, previous):
            raise ValueError("refreshed tree differs from prefix")
        branch = _name(self.base, self.index + 1)
        if ctx.now().timestamp() - self.born >= DEADLINE_SECONDS:
            raise ValueError("three CI rounds elapsed")
        if ctx.git.remote_branch_sha(branch) != prefix:
            raise ValueError("candidate ref changed")
        pr = self.pr
        if pr is None:
            self.pending = True
            raise ci_mode.MissingComparison("prefix PR not registered")
        self.pr = pr
        try:
            row = ctx.tracker.get_pr(pr)
        except TrackerError as exc:
            if not exc.is_not_found:
                raise
            self.pr, self.pending = None, True
            raise ci_mode.MissingComparison("prefix PR not registered") from exc
        if (row.head_sha != prefix or row.head != branch
                or row.base != ctx.config.base_branch or row.state != "open" or row.merged_at):
            raise ValueError("prefix PR changed")
        status = ctx.tracker.pr_checks(pr)
        if status == "fail":
            raise ValueError("prefix CI red")
        self.pending = status in {"pending", "none"}
        if self.pending:
            raise ci_mode.MissingComparison("prefix CI pending")
        before = (previous, ctx.tracker.pr_acceptance_reports(previous)) if self.index else None
        return self.base, prefix, ctx.tracker.pr_acceptance_reports(prefix), before

    def current(self, n, ctx, git, pr, head, base_sha, status):
        self.pending = False
        try:
            if status == "fail":
                raise ValueError("member CI red")
            attempt = SimpleNamespace(n=n, ctx=ctx, git=git, target_branch=ctx.config.base_branch,
                                      log=ctx.store.log_path(n))
            if self.accepted is None:
                self.accepted = ci_mode.require_comparison(attempt, pr, head, base_sha=base_sha, status=status,
                                                           wait=False, source=self)
            else:
                self.measurement(attempt, head, base_sha)
            if not self.accepted.current(n, ctx, git, pr, head, base_sha, self.accepted.status):
                raise ValueError("prefix comparison changed")
            return True
        except Exception as exc:
            if isinstance(exc, TrackerError) and exc.waits:
                raise
            if not self.pending:
                self.reason = str(exc) or type(exc).__name__
            return False  # Evidence failure always leaves the ordinary refresh path available.

    def finish(self, ctx, report, job):
        if (self.reason or job["phase"] not in {"waiting", "publishing", "merged"}
                or job["phase"] == "merged" and self.index + 1 == len(self.chain)):
            _close(ctx, report, self.base, self.chain, self.reason or f"single path {job['phase']}",
                   used=self.index + int(job["phase"] == "merged" and not self.reason))


def prepare(ctx, report, candidates):
    """Discover from refs, or publish all prefixes before polling any CI."""
    base, chain, prs = None, [], {}
    try:
        retired = [json.loads(line) for line in ctx.git._call([
            "for-each-ref", "--format=%(contents:trailers:key=Foragent-Batch,valueonly)", SPENT]).splitlines()
                   if line.strip()]
        recovered, damaged = _discover(ctx, report)
        if damaged or len(recovered) > 1:
            for base, chain in recovered:
                _close(ctx, report, base, chain, "published prefix ledgers changed")
            return None
        if recovered:
            base, chain = recovered[0]
            if any(row["candidate"] == chain[-1][2] for row in retired):
                raise ValueError("candidate already retired")
        else:
            spent = {(m["n"], m["started_at"]) for row in retired for m in row["members"]}
            base = ctx.git.ref_sha(f"origin/{ctx.config.base_branch}")
            selected = [(p, j) for p, j, _ in candidates if (p.n, p.started_at) not in spent
                        and j["phase"] == "waiting" and j["target"] == ctx.config.base_branch][:ctx.config.merge_batch_size]
            if len(selected) < 2:
                return None
            excluded = sorted(set(_baseline_exclusions(ctx, base, ctx.store.log_path(selected[0][0].n))))
            # Keep all selected identities even if construction conflicts before a prefix exists.
            chain = [(p.n, j["head"], base, p.started_at) for p, j in selected]
            before = base
            for k, (p, j) in enumerate(selected):
                if ctx.git.is_ancestor(j["head"], before):
                    raise ValueError("member already reachable")
                tree = ctx.git._call(["merge-tree", "--write-tree", before, j["head"]]).splitlines()[0]
                message = (f"Merge member #{p.n}\n\nForagent-Member: #{p.n} {j['head']} {p.started_at}\n"
                           f"{EXCLUSIONS_TRAILER}: {json.dumps(excluded, separators=(',', ':'))}")
                before = ctx.git._call(["-c", "user.name=foragent", "-c", "user.email=foragent@localhost",
                                        "commit-tree", tree, "-p", before, "-p", j["head"], "-m", message]).strip()
                chain[k] = (p.n, j["head"], before, p.started_at)
            for k, (_, _, prefix, _) in enumerate(chain, 1):
                branch = _name(base, k)
                ctx.git._call(["push", f"--force-with-lease=refs/heads/{branch}:",
                               "origin", f"{prefix}:refs/heads/{branch}"])
        born = int(ctx.git._call(["show", "-s", "--format=%ct", chain[0][2]]))
        if ctx.now().timestamp() - born >= DEADLINE_SECONDS:
            raise ValueError("three CI rounds elapsed")
        available = {(p.n, p.started_at) for p, _, _ in candidates}
        index = next((i for i, (n, _, _, started) in enumerate(chain) if (n, started) in available), None)
        if index is None:
            raise ValueError("all members left the single path")
        for k in range(1, len(chain) + 1):
            prs[k - 1] = _prefix_pr(ctx, base, k)
        return Evidence(base, chain, index, prs, born)
    except Exception as exc:
        if isinstance(exc, TrackerError) and exc.waits:
            raise
        if chain:
            _close(ctx, report, base, chain, str(exc) or type(exc).__name__)
        else:
            report.actions.append(f"merge batch unavailable: {exc}")
        return None
