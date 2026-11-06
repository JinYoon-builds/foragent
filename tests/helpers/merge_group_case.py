"""Shared offline helpers for unit tests."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import shlex
import sys
from threading import Condition, Event, RLock, local
from types import SimpleNamespace

import pytest

from conftest import ACCEPTANCE, git_command
from foragent import board, runner
from foragent.issue import body_hash
from foragent.state import StateStore

from helpers.publication import publish_case


_QUEUE = "merge-queue.jsonl"
_PYTHON = shlex.quote(sys.executable)


def _queue_bytes(case):
    path = StateStore(case.store.root).root / _QUEUE
    return path.read_bytes() if path.exists() else b""


def _queued(case):
    """Read the r17 wire events from a fresh store, including group/verdict."""
    return [json.loads(line) for line in _queue_bytes(case).splitlines()]


def _owner(case, pr):
    return int(next(item.head for item in case.tracker.created_prs
                    if item.number == pr).rsplit("-", 1)[1])


class _RunnerGone(BaseException):
    """Offline process disappearance; no real process or signal is involved."""


@pytest.fixture
def group_case(publish_case, monkeypatch):
    """Use real runner/check/queue decisions with independently running owners.

    Reuse #437's observations, but keep the actor thread-local. The former
    recursive lock contender cannot work with #712: the first runner keeps
    its FIFO turn until it finishes. Gates delay ready owners, never decide
    their turn or replace any publication/group implementation.
    """
    case = publish_case
    case.worker = local()
    case.probes, case.events, case.holds, case.merges = [], [], [], []
    case.appends, case.record_writes, case.closes, case.board_writes = [], [], [], []
    case.reinstalls, case.outcomes = [], {}
    case.poll_counts = Counter()
    case.review_passes, case.green_heads = {}, {}
    case.ready, case.settled, case.release = {}, {}, {}
    case.members, case.run_numbers = (), ()
    case.dead = set()
    case.armed, case.move_on_ready = False, False
    case.group_base, case.holder = None, None
    case.ci_status = lambda pr, head, poll: "pass"
    case.before_lock = lambda n: None
    case.after_claim = lambda row: None
    case.after_verdict = lambda row: None
    case.on_merged = lambda n: None
    transport_lock, observations = RLock(), RLock()
    changed = Condition()

    def actor():
        return getattr(case.worker, "number", None)

    def wake():
        with changed:
            changed.notify_all()

    def event(kind, **fields):
        with observations:
            row = SimpleNamespace(kind=kind, issue=actor(), at=case.clock[0],
                                  holder=case.holder if case.holder == actor() else None, **fields)
            case.events.append(row)
            return row

    def advance(seconds):
        with observations:
            case.clock[0] += timedelta(seconds=seconds)

    @contextmanager
    def external():
        """A tracker/scheduler event, not a write attributed to a runner."""
        previous = actor()
        case.worker.number = None
        try:
            yield
        finally:
            case.worker.number = previous

    transport = case.git._run

    def serialized_transport(args):
        with transport_lock:
            return transport(args)

    monkeypatch.setattr(case.git, "_run", serialized_transport)
    write = StateStore._atomic_write

    def observe_write(store, path, content):
        if Path(path).parent == case.store.root / "runs" and actor() is not None:
            case.record_writes.append((actor(), int(Path(path).stem)))
        return write(store, path, content)

    state_lock = StateStore._state_lock

    @contextmanager
    def observe_queue(store):
        if store.root != case.store.root:
            with state_lock(store):
                yield
            return
        with state_lock(store):
            before = _queue_bytes(case)
            yield
            after = _queue_bytes(case)
            assert after.startswith(before), "merge-queue.jsonl must only grow, byte for byte"
            appended = [json.loads(line) for line in after[len(before):].splitlines()]
            for row in appended:
                if row["event"] == "group":
                    previous = [old for old in _queued(case)[:_queued(case).index(row)]
                                if old["event"] == "group"]
                    done = {old["id"] for old in _queued(case)[:_queued(case).index(row)]
                            if old["event"] == "verdict"}
                    for claim in previous:
                        leader = store.get_run(claim["leader"])
                        if claim["id"] not in done and leader is not None and leader.status == "running":
                            assert not ({m["n"] for m in claim["members"]}
                                        & {m["n"] for m in row["members"]}), "Live open claims cannot overlap"
                case.appends.append((actor(), row))
            ready = [row for row in _queued(case) if row["event"] == "ready"]
            if case.members and not case.armed and set(case.members) <= {r["n"] for r in ready}:
                case.armed = True
                if case.move_on_ready:
                    with transport_lock:
                        case.history.update({"src/epoch.py": "EPOCH = 1\n"})
                case.group_base = case.history.tip
        # Delay outside the short state lock. All runners have independently
        # verified and published their exact ready heads before being released.
        for row in appended:
            if row["event"] == "ready" and row["n"] in case.ready:
                n = row["n"]
                if not case.ready[n].is_set():
                    case.ready[n].set()
                    case.settled[n].set()
                    assert case.release[n].wait(45), f"Ready owner {n} was not released"
            elif row["event"] == "group":
                case.after_claim(row)
            elif row["event"] == "verdict":
                case.after_verdict(row)
        wake()

    merge_lock = case.store.merge_lock

    @contextmanager
    def observe_lock():
        n = actor()
        event("lock-request")
        case.before_lock(n)
        with merge_lock():
            assert case.holder is None
            pr = case.tracker.find_pr(f"foragent/issue-{n}")
            head = case.tracker.pr_head_sha(pr) if pr is not None else None
            hold = SimpleNamespace(issue=n, start=case.clock[0], end=None,
                                   green=head is not None and case.green_heads.get(pr) == head)
            case.holds.append(hold)
            case.holder = n
            event("lock-acquired")
            try:
                yield
            finally:
                hold.end = case.clock[0]
                event("lock-released")
                case.holder = None

    check = runner._check

    def observe_check(command, worktree, *args, **kwargs):
        root = Path(worktree)
        present = frozenset(n for n in case.members if (root / f"src/member_{n}.py").exists())
        event("verify")
        advance(1)
        row = SimpleNamespace(command=command, present=present, armed=case.armed,
                              actor=actor(), before_merges=len(case.merges),
                              holder=case.holder if case.holder == actor() else None,
                              worktree=root, result=None,
                              detached=git_command(root, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD",
                              epoch=(root / "src/epoch.py").read_text())
        case.probes.append(row)
        row.result = check(command, worktree, *args, **kwargs)
        return row.result

    checks = case.tracker.pr_checks

    def poll(pr):
        head = case.tracker.pr_head_sha(pr)
        case.poll_counts[pr, head] += 1
        status = case.ci_status(pr, head, case.poll_counts[pr, head])
        case.tracker.set_pr_checks(pr, status)
        if status == "pass":
            case.green_heads[pr] = head
        event("ci", pr=pr, head=head, status=checks(pr))
        return checks(pr)

    def review(kwargs):
        advance(11)
        case.review_passes[actor()] = case.clock[0]
        return "VERDICT: PASS"

    def sleep(seconds):
        if actor() in case.dead:
            raise _RunnerGone()
        # Real condition wait yields to the independently progressing owner;
        # it grants no turn and does not advance the fake time budget by 30s.
        event("sleep", seconds=seconds)
        with changed:
            changed.wait(timeout=0.02)

    merge = case.tracker.merge_pr

    def squash(pr, *, expected_head_sha, method="squash", **kwargs):
        assert case.holder == actor(), "The final merge must hold its owner's merge.lock"
        assert case.green_heads.get(pr) == expected_head_sha
        assert case.tracker.pr_head_sha(pr) == expected_head_sha
        assert checks(pr) == "pass"
        with transport_lock:
            before = git_command(case.history.origin, "rev-parse", "main")
            tree = git_command(case.history.origin, "merge-tree", "--write-tree",
                               before, expected_head_sha).splitlines()[0]
            merge(pr, expected_head_sha=expected_head_sha, method=method, **kwargs)
            # Model a real squash without executing commit/push/checkout.
            stream = ["commit refs/heads/main\n",
                      "committer Tests <test@example.com> 1792483200 +0000\n",
                      "data 6\nSquash\n", f"from {before}\n", "deleteall\n"]
            for entry in git_command(case.history.origin, "ls-tree", "-r", "-z", tree).split("\0"):
                if entry:
                    metadata, name = entry.split("\t", 1)
                    mode, _, sha = metadata.split()
                    stream.append(f"M {mode} {sha} {json.dumps(name)}\n")
            git_command(case.history.origin, "fast-import", "--quiet", "--force",
                        input="".join(stream) + "\ndone\n")
            case.history.tip = git_command(case.history.origin, "rev-parse", "main")
        advance(2)
        case.merges.append(event("merge", pr=pr, head=expected_head_sha, base=before,
                                 queue=_queue_bytes(case)))
        case.on_merged(actor())
        wake()

    close = case.tracker.close_issue

    def close_issue(n, *args, **kwargs):
        case.closes.append((actor(), n))
        return close(n, *args, **kwargs)

    sync = board.sync_status

    def sync_status(ctx, issue, status, **kwargs):
        n = issue if isinstance(issue, int) else issue.number
        case.board_writes.append((actor(), n, status))
        return sync(ctx, issue, status, **kwargs)

    def after_merge(ctx, pr, base):
        case.reinstalls.append((actor(), _owner(case, pr), len(case.merges)))

    case.external, case.wake = external, wake
    case.review = review
    case.ctx.sleep = sleep
    monkeypatch.setattr(StateStore, "_atomic_write", observe_write)
    monkeypatch.setattr(StateStore, "_state_lock", observe_queue)
    monkeypatch.setattr(case.store, "merge_lock", observe_lock)
    monkeypatch.setattr(runner, "_check", observe_check)
    monkeypatch.setattr(case.tracker, "pr_checks", poll)
    monkeypatch.setattr(case.tracker, "merge_pr", squash)
    monkeypatch.setattr(case.tracker, "close_issue", close_issue)
    monkeypatch.setattr(board, "sync_status", sync_status)
    monkeypatch.setattr(runner, "after_merge", after_merge)
    return case


def prepare(case, numbers=(1, 4, 5), *, maximum=None, culprits=(), own_red=(), interactions=()):
    case.members = case.run_numbers = tuple(numbers)
    case.move_on_ready = bool(culprits or interactions)
    if maximum is not None:
        case.ctx.config = replace(case.ctx.config)
        object.__setattr__(case.ctx.config, "merge_group_max", maximum)
    probe = (
        "from pathlib import Path\nimport runpy, sys\n"
        "n = int(sys.argv[1])\n"
        "member = runpy.run_path(f'src/member_{n}.py')\n"
        "epoch = runpy.run_path('src/epoch.py')['EPOCH']\n"
        "assert not member['OWN_RED'], f'own check failed: {n}'\n"
        "assert not (member['CULPRIT'] and epoch), f'base-move culprit: {n}'\n"
        f"interactions = {tuple(tuple(part) for part in interactions)!r}\n"
        "assert not (epoch and any(all(Path(f'src/member_{m}.py').exists() for m in part) "
        "for part in interactions)), 'member interaction'\n"
    )
    case.history.update({"tools/group_probe.py": probe, "src/epoch.py": "EPOCH = 0\n"})
    git_command(case.root, "fetch", "-q", "origin")
    git_command(case.root, "merge", "-q", "--ff-only", "origin/main")
    case.base_sha = case.history.tip
    approval = case.store.load_approval(1)
    for number in numbers:
        body = ("<!-- foragent:v1 -->\n## Goal\nIndependent member.\n"
                "## Out of scope\nOther features.\n## Done when\n"
                f"```sh\n{_PYTHON} tools/group_probe.py {number}\n```\n"
                f"```sh\ntest -f {ACCEPTANCE}\n```\n")
        if number == 1:
            case.tracker.edit_body(number, body)
        else:
            case.tracker.add_issue(number, f"Member {number}", body, labels=["approved", "running"])
            case.store.start_run(number, branch=f"foragent/issue-{number}",
                                 worktree=case.store.worktree_path(number))
        case.store.save_approval(number, body=body, body_sha=body_hash(body),
                                 acceptance_blobs=approval.acceptance_blobs,
                                 approved_by="human", approved_at=case.clock[0].isoformat())

    def implement(kwargs):
        n = case.worker.number
        root = kwargs["worktree"]
        (root / f"src/member_{n}.py").write_text(
            f"CULPRIT = {n in culprits}\nOWN_RED = {n in own_red}\n")
        (root / f"tests/test_member_{n}.py").write_text(
            "from pathlib import Path\n\ndef test_member():\n"
            f"    assert Path('src/member_{n}.py').is_file()\n")

    case.implementer.steps = [implement] * len(numbers)
    case.reviewer.steps = [case.review] * len(numbers)
    settings = "repo: owner/repo\nreview: fast\n"
    if maximum is not None:
        settings += f"merge_group_max: {maximum}\n"
    (case.root / ".foragent.yml").write_text(settings)


def contend(case, *, before_acquire=None):
    """Admit real ready runners in FIFO order, then let #712 choose turns."""
    case.initial_queue = _queue_bytes(case)
    if before_acquire is not None:
        case.before_lock = before_acquire
    for n in case.run_numbers:
        case.ready[n], case.settled[n], case.release[n] = Event(), Event(), Event()

    def run(n):
        case.worker.number = n
        try:
            try:
                return runner.run_issue(n, case.ctx)
            except _RunnerGone:
                return "crashed"
        finally:
            case.settled[n].set()
            case.wake()

    with ThreadPoolExecutor(max_workers=len(case.run_numbers)) as pool:
        futures = {}
        try:
            for n in case.run_numbers:
                futures[n] = pool.submit(run, n)
                assert case.settled[n].wait(15), f"Run {n} neither became ready nor finished"
                if n in case.members:
                    assert case.ready[n].is_set(), (
                        f"Run {n} did not publish readiness: {futures[n].result(timeout=5)}\n"
                        + case.store.log_path(n).read_text()
                    )
        finally:
            for gate in case.release.values():
                gate.set()
            case.wake()
        case.outcomes = {n: future.result(timeout=30) for n, future in futures.items()}
    assert "error" not in case.outcomes.values(), {
        n: case.store.log_path(n).read_text() for n, result in case.outcomes.items() if result == "error"
    }
