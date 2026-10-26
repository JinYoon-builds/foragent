"""Acceptance test for merge-group-join. Locked by Foragent; do not edit."""

from collections import Counter
from dataclasses import replace
import json
from pathlib import Path
import shlex
import sys
from types import SimpleNamespace

import pytest

from conftest import ACCEPTANCE, git_command
from foragent import runner
from foragent.config import load_config
from foragent.issue import body_hash
from foragent.state import StateStore
from test_base_move_reuse import MERGED, UNOWNED, publish_case  # noqa: F401
from test_short_merge_lock import (  # noqa: F401
    assert_merge_facts, assert_merged, assert_short_holds, run, short_case,
    slow_under_lock,
)
from test_skills_autosync import isolated_home  # noqa: F401
from test_state_split import offline_acceptance  # noqa: F401


QUEUE = "merge-queue.json"
PYTHON = shlex.quote(sys.executable)


def queued(case):
    """Read durable state through a fresh store, never the producer's memory.

    Chosen wire contract: entries in FIFO order, each with number/head_sha;
    implementations may persist additional inputs and coordination fields.
    """
    path = StateStore(case.store.root).root / QUEUE
    return json.loads(path.read_text())["entries"] if path.exists() else []


@pytest.fixture
def group_case(short_case, monkeypatch):
    """Extend the imported fixture's tracker/time/check observations only.

    Keep real verification, publication, queue/lock and runner decisions. The
    tracker adapter models squash results: advancing main to an independent
    PR head (the single-PR fixture's shortcut) would erase earlier members.
    """
    case = short_case
    case.probes, case.queue_reads, case.queue_writes, case.outcomes = [], [], [], {}
    case.armed = False
    case.members = ()
    case.move_on_ready = False
    case.group_base = None
    case.maximum = None
    write = case.store._atomic_write

    def observe_write(path, content):
        write(path, content)
        if Path(path) == case.store.root / QUEUE:
            entries = queued(case)
            case.queue_writes.append(entries)
            if not case.armed and set(case.members) <= {row["number"] for row in entries}:
                case.armed = True
                if case.move_on_ready:
                    case.history.update({"src/epoch.py": "EPOCH = 1\n"})
                    case.group_base = case.history.tip

    check = runner._check

    def observe_check(command, worktree, *args, **kwargs):
        present = frozenset(number for number in case.members
                            if (Path(worktree) / f"src/member_{number}.py").exists())
        # Also observe new group check code that does not call the old
        # _verify_implementation entry point used by short_case's spy.
        case.events.append(SimpleNamespace(kind="verify", issue=case.active,
                                           at=case.clock[0], holder=case.holder))
        case.advance(1)
        row = SimpleNamespace(command=command, present=present, armed=case.armed,
                              actor=case.active, before_merges=len(case.merges),
                              holder=case.holder, result=None,
                              epoch=(Path(worktree) / "src/epoch.py").read_text())
        case.probes.append(row)
        row.result = check(command, worktree, *args, **kwargs)
        return row.result

    merge = case.tracker.merge_pr

    def squash(pr, *, expected_head_sha, method="squash"):
        before = git_command(case.history.origin, "rev-parse", "main")
        tree = git_command(case.history.origin, "merge-tree", "--write-tree",
                           before, expected_head_sha).splitlines()[0]
        merge(pr, expected_head_sha=expected_head_sha, method=method)
        # Import the real combined tree without git commit/push/checkout.
        stream = ["commit refs/heads/main\n",
                  "committer Tests <test@example.com> 1792483200 +0000\n",
                  "data 6\nSquash\n", f"from {before}\n", "deleteall\n"]
        for entry in git_command(case.history.origin, "ls-tree", "-r", "-z", tree).split("\0"):
            if entry:
                metadata, name = entry.split("\t", 1)
                mode, _, sha = metadata.split()
                stream.append(f"M {mode} {sha} {json.dumps(name)}\n")
        # The imported single-PR tracker just pointed main at the PR head;
        # this fixture's squash commit instead has the previous main as parent.
        git_command(case.history.origin, "fast-import", "--quiet", "--force",
                    input="".join(stream) + "\ndone\n")
        case.history.tip = git_command(case.history.origin, "rev-parse", "main")

    monkeypatch.setattr(case.store, "_atomic_write", observe_write)
    monkeypatch.setattr(runner, "_check", observe_check)
    monkeypatch.setattr(case.tracker, "merge_pr", squash)
    return case


def prepare(case, numbers=(1, 4, 5), *, maximum=None, culprits=(), own_red=()):
    case.members = tuple(numbers)
    case.maximum = maximum
    case.move_on_ready = bool(culprits)
    # The optional field does not yet exist in Config. Inject it so absence of
    # the parser cannot turn these behavioural cases into setup errors; the
    # public YAML loader is checked separately by assert_config below.
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
        number = case.active
        root = kwargs["worktree"]
        (root / f"src/member_{number}.py").write_text(
            f"CULPRIT = {number in culprits}\nOWN_RED = {number in own_red}\n")
        # A distinct changed related unit per member makes omissions observable.
        (root / f"tests/test_member_{number}.py").write_text(
            "from pathlib import Path\n\ndef test_member():\n"
            f"    assert Path('src/member_{number}.py').is_file()\n")

    case.implementer.steps = [implement] * len(numbers)
    case.reviewer.steps = [case.review] * len(numbers)
    settings = "repo: owner/repo\nreview: fast\n"
    if maximum is not None:
        settings += f"merge_group_max: {maximum}\n"
    (case.root / ".foragent.yml").write_text(settings)


def contend(case, *, before_acquire=None):
    pending = list(case.members[1:])

    def let_another_run_go_first():
        case.queue_reads.append((case.active, queued(case)))
        while pending:
            number = pending.pop(0)
            case.outcomes[number] = run(case, number)
        if before_acquire is not None:
            before_acquire()

    case.before_lock = let_another_run_go_first
    case.outcomes[case.members[0]] = run(case, case.members[0])
    assert "error" not in case.outcomes.values(), {
        n: case.store.log_path(n).read_text() for n, result in case.outcomes.items() if result == "error"
    }


def owner(case, pr):
    return int(next(item.head for item in case.tracker.created_prs if item.number == pr).rsplit("-", 1)[1])


def merge_order(case):
    return [owner(case, row.pr) for row in case.merges]


def group_rows(case, members):
    # A failed member's later solo refresh can also contain all survivors'
    # source files. It is continuation, not a repeat of the original proposal.
    return [row for row in case.probes if row.armed and row.before_merges == 0
            and row.present == frozenset(members)]


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
    assert all(not row.result.timed_out for row in rows)
    assert all(row.result.exit_code == 0 for row in rows) is passed


def assert_facts(case, number, size, extra=0):
    saved = json.loads((case.store.root / "runs" / f"{number}.json").read_text())
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
    # short_case attributes a merge to its actor, which is now the leader.
    # Its existing assert_merged helper needs the PR owner's event projection.
    view = SimpleNamespace(**vars(case))
    view.merges = [SimpleNamespace(**{**vars(row), "issue": owner(case, row.pr)}) for row in case.merges]
    for number in numbers:
        assert case.outcomes[number] == "merged"
        assert_merged(view, number)
        assert len([pr for pr in case.tracker.merged_prs if owner(case, pr) == number]) == 1
        assert f"src/member_{number}.py" in git_command(case.history.origin, "ls-tree", "-r", "--name-only", "main").splitlines()


def assert_group_short_holds(case):
    # #437 allows one non-waiting CI reread per PR. Extend its one-PR event
    # projection to distinct group PRs; a repeat read of any PR still fails.
    view = SimpleNamespace(**vars(case))
    view.events = []
    seen = set()
    for row in case.events:
        if row.kind == "lock-acquired":
            seen = set()
        if row.kind == "ci" and row.holder is not None:
            if row.pr not in seen and seen:
                view.events.append(SimpleNamespace(**{**vars(row), "kind": "member-ci-reread"}))
                seen.add(row.pr)
                continue
            seen.add(row.pr)
        view.events.append(row)
    # A member whose PR a group leader already merged still acquires its own
    # merge.lock request, then finishes merged (#459). Its PR is closed by then,
    # so that hold cannot see a green open PR; it must stay empty (no merge in it).
    merged_at = {owner(case, row.pr): row.at for row in case.merges}
    view.holds = [SimpleNamespace(**{**vars(hold), "green": True})
                  if not hold.green and hold.issue in merged_at and merged_at[hold.issue] <= hold.start
                  else hold for hold in case.holds]
    assert not slow_under_lock(view)
    assert_short_holds(view)


def assert_config(case, maximum):
    assert getattr(load_config(case.root, global_path=case.root / "no-global.yml"),
                   "merge_group_max", None) == maximum


def test_default_group_checks_union_once_and_finishes_borrowed_members(group_case):
    case = group_case
    prepare(case)
    contend(case)
    assert_suite(group_rows(case, case.members), case.members)
    assert_config(case, 4)
    assert merge_order(case) == list(case.members), "Merge in durable FIFO order, including the leader"
    assert_members_merged(case, case.members)
    for number in case.members:
        assert_facts(case, number, 3)
    # Proposals may have checked earlier, smaller groups before the last run
    # arrived; after the final group's PASS no member checks again.
    last_check = max(case.probes.index(row) for row in group_rows(case, case.members))
    assert case.probes[last_check + 1:] == []
    assert_group_short_holds(case)


def test_queue_is_durable_before_lock_and_nondefault_max_reserves_leader_slot(group_case):
    case = group_case
    prepare(case, (1, 4, 5, 6), maximum=2)
    contend(case)
    first = next((entries for actor, entries in case.queue_reads if actor == 1), [])
    assert [row["number"] for row in first] == [1], "A ready run must persist merge-queue.json before requesting merge.lock"
    for _, entries in case.queue_reads:
        for entry in entries:
            pr = next(pr for pr in case.tracker.created_prs if pr.head == f"foragent/issue-{entry['number']}")
            assert entry["head_sha"] == case.tracker.pr_head_sha(pr.number)
    assert merge_order(case) == [1, 6, 4, 5]
    assert_members_merged(case, case.members)
    for number in case.members:
        assert_facts(case, number, 2)
    # At most two unmerged members in any proposed joint check.
    for row in case.probes:
        merged = {owner(case, m.pr) for m in case.merges[:row.before_merges]}
        assert len(row.present - merged) <= 2
    assert_config(case, 2)
    assert_group_short_holds(case)


def test_max_one_preserves_individual_base_move_recheck_and_records_singletons(group_case):
    case = group_case
    prepare(case, (1, 4), maximum=1)
    contend(case)
    assert_members_merged(case, case.members)
    assert merge_order(case) == [4, 1]
    done = [row for row in case.probes if "tools/group_probe.py" in row.command]
    assert Counter(row.actor for row in done) == Counter({1: 2, 4: 1}), "The second singleton rechecks after a result-affecting base move"
    assert_facts(case, 1, 1)
    assert_facts(case, 4, 1)
    assert_config(case, 1)
    for number in case.members:
        assert_merge_facts(case, number)
    assert_group_short_holds(case)


def test_own_failed_checks_red_ci_and_holds_are_excluded(group_case):
    case = group_case
    prepare(case, (1, 4, 5, 6, 7, 8, 9), own_red=(4,))
    # Only 1 and 9 can ever be ready. Invalid members are exercised through
    # the real runner, not seeded into a synthetic queue.
    case.members = (1, 9)
    case.tracker.add_labels(5, ["risk:high"])
    case.tracker.add_labels(6, ["needs-human"])
    case.ci_status = lambda pr, head, poll: "fail" if owner(case, pr) == 7 else "pass"
    poll = case.ci_status

    def changed_head(pr, head, count):
        if owner(case, pr) == 8:
            case.tracker.set_pr_head_sha(pr, "f" * 40)
        return poll(pr, head, count)

    case.ci_status = changed_head
    pending = [4, 5, 6, 7, 8]
    started = []

    def reject_others():
        if started:
            return
        started.append(True)
        while pending:
            n = pending.pop(0)
            case.outcomes[n] = run(case, n)
        # Disable this hook before the good contender requests its lock.
        case.before_lock = lambda: None
        case.outcomes[9] = run(case, 9)

    case.before_lock = reject_others
    case.outcomes[1] = run(case)
    assert "error" not in case.outcomes.values(), {
        n: case.store.log_path(n).read_text() for n, result in case.outcomes.items() if result == "error"
    }
    assert_suite(group_rows(case, (1, 9)), (1, 9))
    assert_members_merged(case, (1, 9))
    assert merge_order(case) == [1, 9]
    assert case.outcomes[4] == "retries-exceeded"
    assert case.outcomes[7] == "ci-failed"
    assert all(case.outcomes[n] == "held" for n in (5, 6, 8))
    for number in (4, 5, 6, 7, 8):
        assert case.tracker.get_issue(number).state == "open"
        assert all(number not in {entry["number"] for entry in rows} for rows in case.queue_writes)
    for number in (1, 9):
        assert_facts(case, number, 2)
    assert_group_short_holds(case)


def test_leader_rereads_borrowed_members_ci_at_final_lock(group_case):
    case = group_case
    prepare(case, (1, 4))
    changed = []

    def invalidate_borrowed_member():
        if case.active != 4 or changed:
            return
        changed.append(True)
        case.ci_status = lambda pr, head, poll: "fail" if owner(case, pr) == 1 else "pass"

    contend(case, before_acquire=invalidate_borrowed_member)
    assert changed, "Exercise the change after the joint check, before acquisition"
    assert_suite(group_rows(case, (1, 4)), (1, 4))
    assert merge_order(case) == [4], "The leader must not merge a borrowed member whose CI rerun failed"
    assert_members_merged(case, (4,))
    assert case.tracker.get_issue(1).state == "open"
    assert case.outcomes[1] == "ci-failed"
    assert_group_short_holds(case)


def test_joint_failure_never_merges_the_culprit_and_still_merges_the_survivor(group_case):
    case = group_case
    prepare(case, (1, 4), culprits=(1,))
    contend(case)
    assert_suite(group_rows(case, case.members), case.members, passed=False)
    assert merge_order(case) == [4], "Never merge the culprit; still merge the survivor"
    assert_members_merged(case, (4,))
    assert case.outcomes[1] == "retries-exceeded"
    assert case.store.get_run(1).outcome == "retries-exceeded"
    assert case.store.get_run(1).attempts == 1
    assert case.tracker.get_issue(1).state == "open"
    assert "blocked" in case.tracker.get_issue(1).labels
    assert "base-move culprit: 1" in case.store.log_path(1).read_text()
    assert len(case.implementer.calls) == len(case.reviewer.calls) == 2
    # The invariant survives adding bisect: fallback has no bisect suites;
    # splitting this pair checks [1] and [4], with no recombination needed.
    saved = json.loads((case.store.root / "runs" / "4.json").read_text())
    extra = saved.get("merge_group_bisect_checks")
    assert extra in (0, 2), f"Expected fallback (0) or two-member bisect (2); got {extra}"
    assert_facts(case, 4, 1, extra)
    assert_group_short_holds(case)
