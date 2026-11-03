"""Acceptance test for merge-group-join. Locked by Foragent; do not edit."""

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
from foragent.config import load_config
from foragent.issue import body_hash
from foragent.state import StateStore
from test_base_move_reuse import MERGED, UNOWNED, publish_case  # noqa: F401
from test_short_merge_lock import (  # noqa: F401
    assert_merge_facts, assert_merged, assert_short_holds, short_case,
)
from test_skills_autosync import isolated_home  # noqa: F401
from test_state_split import offline_acceptance  # noqa: F401


QUEUE = "merge-queue.jsonl"
PYTHON = shlex.quote(sys.executable)


def queue_bytes(case):
    path = StateStore(case.store.root).root / QUEUE
    return path.read_bytes() if path.exists() else b""


def queued(case):
    """Read the r17 wire events from a fresh store, including group/verdict."""
    return [json.loads(line) for line in queue_bytes(case).splitlines()]


def owner(case, pr):
    return int(next(item.head for item in case.tracker.created_prs
                    if item.number == pr).rsplit("-", 1)[1])


class RunnerGone(BaseException):
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
            before = queue_bytes(case)
            yield
            after = queue_bytes(case)
            assert after.startswith(before), "merge-queue.jsonl must only grow, byte for byte"
            appended = [json.loads(line) for line in after[len(before):].splitlines()]
            for row in appended:
                if row["event"] == "group":
                    previous = [old for old in queued(case)[:queued(case).index(row)]
                                if old["event"] == "group"]
                    done = {old["id"] for old in queued(case)[:queued(case).index(row)]
                            if old["event"] == "verdict"}
                    for claim in previous:
                        leader = store.get_run(claim["leader"])
                        if claim["id"] not in done and leader is not None and leader.status == "running":
                            assert not ({m["n"] for m in claim["members"]}
                                        & {m["n"] for m in row["members"]}), "Live open claims cannot overlap"
                case.appends.append((actor(), row))
            ready = [row for row in queued(case) if row["event"] == "ready"]
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
            raise RunnerGone()
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
                                 queue=queue_bytes(case)))
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
        case.reinstalls.append((actor(), owner(case, pr), len(case.merges)))

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
                f"```sh\n{PYTHON} tools/group_probe.py {number}\n```\n"
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
    case.initial_queue = queue_bytes(case)
    if before_acquire is not None:
        case.before_lock = before_acquire
    for n in case.run_numbers:
        case.ready[n], case.settled[n], case.release[n] = Event(), Event(), Event()

    def run(n):
        case.worker.number = n
        try:
            try:
                return runner.run_issue(n, case.ctx)
            except RunnerGone:
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


def merge_order(case):
    return [owner(case, row.pr) for row in case.merges]


def group_rows(case, members):
    return [row for row in case.probes if row.armed and row.before_merges == 0
            and row.present == frozenset(members)
            and row.worktree not in {case.store.worktree_path(n) for n in case.run_numbers}]


def assert_suite(rows, members, *, passed=True):
    done = [row for row in rows if "tools/group_probe.py" in row.command]
    assert Counter(int(shlex.split(row.command)[-1]) for row in done) == Counter(members), (
        f"Expected one joint check with every member's Done when; members={list(members)}, "
        f"observed={[row.command for row in done]}"
    )
    baseline = [row for row in rows if f"{MERGED}::test_merged" in row.command]
    assert len(baseline) == 1 and f"{UNOWNED}::test_unowned" in baseline[0].command
    units = [row for row in rows if "tests/test_member_" in row.command]
    assert len(units) == 1, "Related units run once for the union of members"
    assert {word for word in shlex.split(units[0].command) if word.startswith("tests/test_member_")} == {
        f"tests/test_member_{n}.py" for n in members
    }
    assert all(row.holder is None for row in rows), "Joint checks run outside merge.lock"
    assert all(row.detached for row in rows), "Joint worktrees are detached"
    assert all(not row.result.timed_out for row in rows)
    assert all(row.result.exit_code == 0 for row in rows) is passed


def assert_facts(case, number, size, extra=0):
    saved = json.loads((StateStore(case.store.root).root / "runs" / f"{number}.json").read_text())
    assert saved.get("merge_group_size") == size, (
        f"Run {number}: merge_group_size must be {size}; got {saved.get('merge_group_size')}"
    )
    assert type(saved.get("merge_group_size")) is int
    assert saved.get("merge_group_bisect_checks") == extra, (
        f"Run {number}: merge_group_bisect_checks must be {extra}; "
        f"got {saved.get('merge_group_bisect_checks')}"
    )
    assert type(saved.get("merge_group_bisect_checks")) is int


def assert_members_merged(case, numbers):
    for n in numbers:
        assert case.outcomes[n] == "merged"
        # No event projection: a merge's observed actor must be its PR owner.
        assert_merged(case, n)
        assert len([pr for pr in case.tracker.merged_prs if owner(case, pr) == n]) == 1
        assert f"src/member_{n}.py" in git_command(
            case.history.origin, "ls-tree", "-r", "--name-only", "main").splitlines()


def assert_group_short_holds(case):
    # Each owner reads only its own CI once; no multi-PR or empty-hold waiver.
    assert_short_holds(case)
    for n in merge_order(case):
        assert_merge_facts(case, n)
    assert all(row.issue == owner(case, row.pr) for row in case.merges), "A runner merges only its own PR"
    assert all(actor == n for actor, n in case.closes), "A runner closes only its own issue"
    assert all(actor == n for actor, n in case.record_writes), "A runner writes only its own run record"
    assert all(actor == n for actor, n, status in case.board_writes), "A runner syncs only its own board status"
    assert queue_bytes(case).startswith(case.initial_queue)
    appended = [json.loads(line) for line in queue_bytes(case)[len(case.initial_queue):].splitlines()]
    assert appended == [row for actor, row in case.appends], "Every event must be appended under the short state lock"
    for actor, row in case.appends:
        if row["event"] in {"ready", "withdrawn", "turn", "merged"}:
            assert actor == row["n"], "Queue membership events belong to their runner"
        elif row["event"] == "group":
            assert actor == row["leader"], "Only the leader appends its group claim"
        elif row["event"] == "verdict":
            group = next(g for g in queued(case) if g["event"] == "group" and g["id"] == row["id"])
            assert actor == group["leader"], "Only the claiming leader appends its verdict"


def assert_group(case, members, *, result="pass", culprits=()):
    groups = [row for row in queued(case) if row["event"] == "group"
              and [member["n"] for member in row["members"]] == list(members)]
    assert len(groups) == 1, "The FIFO turn holder must append one r17 group claim to merge-queue.jsonl"
    group, = groups
    assert group["leader"] == members[0]
    assert group["base_sha"] == case.group_base
    assert group.get("id") and group.get("at")
    ready = {row["n"]: row["head_sha"] for row in queued(case) if row["event"] == "ready"}
    assert group["members"] == [{"n": n, "head_sha": ready[n]} for n in members]
    verdicts = [row for row in queued(case) if row["event"] == "verdict" and row["id"] == group["id"]]
    assert len(verdicts) == 1, "The leader must append one durable verdict for its claim"
    verdict, = verdicts
    assert verdict["result"] == result
    assert verdict["culprits"] == list(culprits)
    rows = queued(case)
    assert rows.index(group) < rows.index(verdict)
    for merge in case.merges:
        if owner(case, merge.pr) in set(members) - set(culprits) and result == "pass":
            assert verdict in [json.loads(line) for line in merge.queue.splitlines()], "Verdict is durable before merge"
            assert merge.head == ready[owner(case, merge.pr)], "Reuse must merge the exact verdict-covered head"
    return group, verdict


def assert_config(case, maximum):
    assert getattr(load_config(case.root, global_path=case.root / "no-global.yml"),
                   "merge_group_max", None) == maximum


def test_default_group_checks_union_once_and_finishes_borrowed_members(group_case):
    case = group_case
    prepare(case)
    contend(case)
    assert_group(case, case.members)
    assert_suite(group_rows(case, case.members), case.members)
    assert_config(case, "auto")  # absent key: auto, which starts at 4
    assert merge_order(case) == list(case.members), "Members merge themselves in durable FIFO order"
    assert_members_merged(case, case.members)
    for n in case.members:
        assert_facts(case, n, 3)
    last_check = max(case.probes.index(row) for row in group_rows(case, case.members))
    assert case.probes[last_check + 1:] == [], "A covered head reuses the joint PASS"
    assert case.reinstalls == [(5, 5, 3)], "Only the last merging member reinstalls, once"
    assert_group_short_holds(case)


def test_queue_is_durable_before_lock_and_nondefault_max_reserves_leader_slot(group_case):
    case = group_case
    prepare(case, (1, 4, 5, 6), maximum=2)
    original = b'{ "n": 90, "event": "withdrawn", "head_sha": "old" }\n'
    (case.store.root / QUEUE).write_bytes(original)
    contend(case)
    assert_group(case, (1, 4))
    groups = [row for row in queued(case) if row["event"] == "group"]
    assert [[m["n"] for m in row["members"]] for row in groups] == [[1, 4], [5, 6]]
    assert queue_bytes(case).startswith(original), "Old bytes must never be normalized or deleted"
    assert not (case.store.root / "merge-queue.json").exists(), "Reuse #712's log, not a second queue"
    assert merge_order(case) == [1, 4, 5, 6]
    assert_members_merged(case, case.members)
    events = queued(case)
    for group in groups:
        for member in group["members"]:
            assert any(row["event"] == "ready" and row["n"] == member["n"]
                       and row["head_sha"] == member["head_sha"] for row in events[:events.index(group)])
            assert_facts(case, member["n"], 2)
    assert case.reinstalls == [(4, 4, 2), (6, 6, 4)]
    assert_config(case, 2)
    assert_group_short_holds(case)


def test_max_one_preserves_individual_base_move_recheck_and_records_singletons(group_case):
    case = group_case
    prepare(case, (1, 4), maximum=1)
    contend(case)
    assert_members_merged(case, case.members)
    assert merge_order(case) == [1, 4], "Max one retains #712's FIFO one-PR path"
    done = [row for row in case.probes if "tools/group_probe.py" in row.command]
    assert Counter(row.actor for row in done) == Counter({1: 1, 4: 2})
    for n in case.members:
        assert_facts(case, n, 1)
    assert not any(row["event"] == "group" for row in queued(case))
    assert_config(case, 1)
    assert case.reinstalls == [(1, 1, 1), (4, 4, 2)]
    assert_group_short_holds(case)


def test_own_failed_checks_red_ci_and_holds_are_excluded(group_case):
    case = group_case
    prepare(case, (1, 4, 5, 6, 7, 8, 9), own_red=(4,))
    case.members = (1, 9)
    case.tracker.add_labels(5, ["risk:high"])
    case.tracker.add_labels(6, ["needs-human"])

    def status(pr, head, poll):
        if owner(case, pr) == 8:
            case.tracker.set_pr_head_sha(pr, "f" * 40)
        return "fail" if owner(case, pr) == 7 else "pass"

    case.ci_status = status
    contend(case)
    assert_group(case, (1, 9))
    assert_suite(group_rows(case, (1, 9)), (1, 9))
    assert_members_merged(case, (1, 9))
    assert merge_order(case) == [1, 9]
    assert case.outcomes[4] == "retries-exceeded"
    assert case.outcomes[7] == "ci-failed"
    assert all(case.outcomes[n] == "held" for n in (5, 6, 8))
    for n in (4, 5, 6, 7, 8):
        assert case.tracker.get_issue(n).state == "open"
        assert not any(row["event"] == "ready" and row["n"] == n for row in queued(case))
    for n in (1, 9):
        assert_facts(case, n, 2)
    assert_group_short_holds(case)


def test_leader_rereads_borrowed_members_ci_at_final_lock(group_case):
    # Historical name retained; r17 moves the member's final CI read to its
    # own runner. The leader must never read another member's CI under lock.
    case = group_case
    prepare(case, (1, 4))
    changed = []

    def invalidate_member(n):
        if n == 4 and not changed:
            changed.append(True)
            case.ci_status = lambda pr, head, poll: "fail" if owner(case, pr) == 4 else "pass"

    contend(case, before_acquire=invalidate_member)
    assert_group(case, (1, 4))
    assert changed
    assert_suite(group_rows(case, (1, 4)), (1, 4))
    assert merge_order(case) == [1]
    assert_members_merged(case, (1,))
    assert case.outcomes[4] == "ci-failed"
    assert case.tracker.get_issue(4).state == "open"
    assert all(owner(case, row.pr) == row.issue for row in case.events
               if row.kind == "ci" and row.holder is not None)
    assert_group_short_holds(case)


def test_joint_failure_never_merges_the_culprit_and_still_merges_the_survivor(group_case):
    case = group_case
    prepare(case, (1, 4), culprits=(1,))
    contend(case)
    # #459 may use solo fallback; #460 may identify the culprit and cover [4].
    groups = [row for row in queued(case) if row["event"] == "group"]
    assert groups, "A failed joint check must still append a claim and a verdict"
    verdict, = [row for row in queued(case) if row["event"] == "verdict" and row["id"] == groups[0]["id"]]
    assert verdict["result"] == "fail" or verdict["culprits"] == [1]
    assert_suite(group_rows(case, case.members), case.members, passed=False)
    assert merge_order(case) == [4]
    assert_members_merged(case, (4,))
    assert case.outcomes[1] == "retries-exceeded"
    assert case.store.get_run(1).attempts == 1
    assert case.tracker.get_issue(1).state == "open"
    assert "blocked" in case.tracker.get_issue(1).labels
    assert "base-move culprit: 1" in case.store.log_path(1).read_text()
    assert len(case.implementer.calls) == len(case.reviewer.calls) == 2
    saved = json.loads((case.store.root / "runs" / "4.json").read_text())
    assert saved.get("merge_group_bisect_checks") in (0, 2)
    assert_facts(case, 4, 1, saved["merge_group_bisect_checks"])
    assert_group_short_holds(case)


def test_changed_member_head_cannot_consume_the_old_pass_verdict(group_case):
    case = group_case
    prepare(case, (1, 4))

    def replace_head(row):
        with case.external():
            case.tracker.set_pr_head_sha(case.tracker.find_pr("foragent/issue-4"), "f" * 40)

    case.after_verdict = replace_head
    contend(case)
    assert_group(case, (1, 4))
    assert merge_order(case) == [1]
    assert case.outcomes[4] == "held"
    assert case.tracker.get_issue(4).state == "open"
    assert_group_short_holds(case)


@pytest.mark.parametrize("move", ["neutral", "source", "reverted-source"])
def test_member_checks_the_whole_base_interval_before_reusing_a_verdict(group_case, move):
    case = group_case
    prepare(case, (1, 4))
    neutral = move == "neutral"

    def move_main(n):
        if n == 1:
            with case.external():
                case.history.update({"docs/group.md" if neutral else "src/outside.py": "VALUE = 1\n"})
                if move == "reverted-source":
                    case.history.update({"src/outside.py": None, "docs/group.md": "Neutral net diff\n"})

    case.on_merged = move_main
    contend(case)
    # A result-affecting outsider requires solo refresh, whose new head is
    # deliberately different from the old verdict-covered head.
    groups = [row for row in queued(case) if row["event"] == "group"]
    assert groups, "Base-interval reuse needs a persisted r17 claim"
    if neutral:
        assert_group(case, (1, 4))
    assert_members_merged(case, (1, 4))
    assert_suite(group_rows(case, (1, 4)), (1, 4))
    later = [row for row in case.probes if row.before_merges and row.actor == 4]
    assert bool(later) is not neutral, "Only neutral moves and exact earlier group merges permit reuse"
    if not neutral:
        assert_facts(case, 4, 1)
    assert_group_short_holds(case)


@pytest.mark.parametrize("dead", [4, 5], ids=["middle-member", "last-member"])
def test_dead_member_is_skipped_and_the_last_live_member_reinstalls(group_case, dead):
    case = group_case
    prepare(case)

    def member_dies(row):
        with case.external():
            case.store.finish_run(dead, outcome="crashed")
        case.dead.add(dead)
        case.wake()

    case.after_verdict = member_dies
    contend(case)
    assert_group(case, (1, 4, 5))
    survivors = tuple(n for n in case.members if n != dead)
    assert merge_order(case) == list(survivors)
    assert_members_merged(case, survivors)
    assert case.tracker.get_issue(dead).state == "open"
    assert case.store.get_run(dead).outcome == "crashed"
    pr = case.tracker.find_pr(f"foragent/issue-{dead}")
    assert pr not in case.tracker.merged_prs
    assert case.reinstalls == [(survivors[-1], survivors[-1], 2)]
    assert not any(row["event"] == "withdrawn" and actor != dead and row.get("n") == dead
                   for actor, row in case.appends)
    assert_group_short_holds(case)


def test_open_claim_expires_from_leader_liveness_without_another_writer(group_case):
    case = group_case
    prepare(case)

    def leader_dies(row):
        if row["leader"] == 1:
            with case.external():
                case.store.finish_run(1, outcome="crashed")
            raise RunnerGone()

    case.after_claim = leader_dies
    contend(case)
    groups = [row for row in queued(case) if row["event"] == "group"]
    assert len(groups) == 2, "A dead leader's open claim must not strand the remaining ready members"
    first, second = groups
    assert first["leader"] == 1 and second["leader"] == 4
    assert [m["n"] for m in second["members"]] == [4, 5]
    assert not any(row["event"] == "verdict" and row["id"] == first["id"] for row in queued(case))
    assert case.tracker.get_issue(1).state == "open"
    assert merge_order(case) == [4, 5]
    assert_members_merged(case, (4, 5))
    assert case.reinstalls == [(5, 5, 2)]
    assert_group_short_holds(case)


@pytest.mark.parametrize("maximum", [0, -1, True, 1.5, "two"])
def test_group_max_rejects_values_other_than_positive_integers(group_case, maximum):
    case = group_case
    prepare(case, (1,), maximum=1024)
    # Check a valid unbounded value too: an unknown-key error must not pass
    # this negative test on main before the feature exists.
    assert_config(case, 1024)
    (case.root / ".foragent.yml").write_text(f"repo: owner/repo\nmerge_group_max: {json.dumps(maximum)}\n")
    with pytest.raises(ValueError, match="merge_group_max"):
        load_config(case.root, global_path=case.root / "no-global.yml")
