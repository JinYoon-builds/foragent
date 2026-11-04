"""Acceptance test for stale-base-refresh: an older measured base is a base move, refreshed once.

When main moves only by result-neutral commits while a run works, the run keeps
its verified head and accepts the new base without a push (#1688). The head
still sits on the older base, and GitHub's acceptance run measures the base that
its pull_request event names: on #1871 (2026-11-04) the report said 3157ea3a,
the head's own fork point, while the gate expected ed7ec76b. Re-running that CI
reuses the same event, so it reports the old base again. Today the gate waits
for fresh reports that never come and holds the issue as
acceptance-baseline-unavailable (#1871, #1473, #1894).

Now a report that names a base OLDER than the expected one (an ancestor of it)
is a base move: Foragent refreshes the PR onto the current base once, through
its existing base-refresh path. That push makes CI measure again, on the new
head, and the gate uses the reports for that head. A second stale report for
the same pair of bases is not refreshed again; today's wait and hold apply.
Missing shards keep today's wait.

Real runner, gate tick, merge owner, Git, state and pytest measurements (the
merge-owner prototype world); only GitHub, models and the clock are fake. The
fake CI starts one round per pushed head and keeps its reports, as a re-run does.
"""

from datetime import timedelta

import pytest

from conftest import git_command
from foragent import merge_owner, runner
from foragent.reasons import Reason
from acceptance.test_merge_path_real_settings import world  # noqa: F401
from test_merge_owner_proto import proto  # noqa: F401
import test_merge_owner_proto as support

N = 5
BRANCH = f"foragent/issue-{N}"
BOOKKEEPING = ".claude/agent-memory/fa-orch/MEMORY.md"


def git(case, *args):
    return git_command(case.history.origin, *args)


def contains(case, ancestor, sha):
    try:
        git(case, "merge-base", "--is-ancestor", ancestor, sha)
    except Exception:  # noqa: BLE001 - git exits 1 for "no"
        return False
    return True


@pytest.fixture
def stale(proto, monkeypatch):
    """Main moves by a bookkeeping commit while the worker implements; CI names an older base.

    `case.payload_base(row)` is the base GitHub's pull_request event names for a pushed
    head. By default it is the base the head was built on: main at the push when the
    head contains it, else the head's fork point from main (what #1871 showed).
    """
    case = proto
    case.old_base = case.history.tip
    case.moved_to = None
    factory = case.ctx.executor_factory

    def moving_factory(name):
        executor = factory(name)
        if name != case.config.executor:
            return executor

        class Implementer:
            def run(self, **kwargs):
                result = executor.run(**kwargs)
                if case.moved_to is None:
                    # Another lane's bookkeeping lands while this worker runs.
                    case.history.update({BOOKKEEPING: "- lesson\n"}, message="Foragent role memory: orchestrator")
                    case.moved_to = case.history.tip
                return result

        return Implementer()

    case.ctx.executor_factory = moving_factory

    def payload_base(row):
        return git(case, "merge-base", row.head, row.base)

    case.payload_base = payload_base
    reports = case.ci.reports

    def measured(row):
        row.base = case.payload_base(row)
        return reports(row)

    monkeypatch.setattr(case.ci, "reports", measured)
    transport = case.git._run
    case.branch_pushes = 0

    def counted_transport(argv):
        args = list(argv)
        while args[:1] == ["-C"]:
            args = args[2:]
        if args[:1] == ["push"] and args[-1].split(":")[-1].removeprefix("refs/heads/") == BRANCH:
            case.branch_pushes += 1
            if case.branch_pushes > 4:
                pytest.fail("The PR branch was pushed again and again: a stale base refreshes at most once")
        return transport(argv)

    monkeypatch.setattr(case.git, "_run", counted_transport)
    case.sleeps = []

    def sleep(seconds):
        # The run's own CI waits advance the fake clock; nothing else sleeps.
        case.sleeps.append(seconds)
        case.clock[0] += timedelta(seconds=seconds)

    case.ctx.sleep = sleep
    return case


def run(case):
    case.clock[0] += timedelta(seconds=1)
    case.store.start_run(N, branch=BRANCH, worktree=case.store.worktree_path(N))
    case.tracker.add_labels(N, ["running"])
    case.worker.number = N
    return runner.run_issue(N, case.ctx)


def pushes(case):
    """Every head CI was started for on this issue's branch, in push order."""
    return [row for row in case.ci.by_head.values() if row.branch == BRANCH]


def log(case):
    path = case.store.log_path(N)
    return path.read_text() if path.exists() else ""


def test_older_measured_base_refreshes_the_pr_once_and_merges_on_the_new_head(stale):
    case = stale

    outcome = run(case)

    assert case.moved_to is not None and case.moved_to != case.old_base
    first = pushes(case)[0]
    assert first.base == case.old_base and not contains(case, case.moved_to, first.head), (
        "Setup: the kept head sits on the older base and CI measured that older base"
    )
    assert outcome == "merge-queued", (
        f"A report on an older base must not hold the issue; got {outcome!r}, "
        f"flag {case.store.load_flag(N)}\n{log(case)[-2000:]}"
    )
    assert case.store.load_flag(N) is None
    rounds = pushes(case)
    assert len(rounds) == 2, (
        f"The older base must refresh the PR exactly once (one new head, one new CI round); "
        f"heads pushed: {[row.head for row in rounds]}"
    )
    assert case.branch_pushes == 2, f"One publication and one refresh push, got {case.branch_pushes}"
    refreshed = rounds[1]
    assert contains(case, case.moved_to, refreshed.head), "The refresh merges the current base into the PR"
    assert contains(case, first.head, refreshed.head), "The refresh keeps the reviewed work"
    assert git(case, "rev-parse", f"refs/heads/{BRANCH}") == refreshed.head
    assert any(head == refreshed.head for _, head, _ in case.ci.reads), (
        "After the refresh the gate reads CI's reports for the new head"
    )
    assert case.model_calls.get(N) == 1, "A base refresh is not a worker turn"

    support.drain(case, count=1)

    assert case.tracker.merged_prs == [support.pr_for(case, N).number], "The refreshed PR merges once"
    assert case.merges[-1]["head"] == refreshed.head, "It merges the head CI measured on the current base"
    assert len(pushes(case)) == 2, "Merging needs no further refresh"
    assert case.tracker.get_issue(N).state == "closed"


def test_a_second_stale_report_for_the_same_bases_is_not_refreshed_again(stale):
    case = stale
    # GitHub keeps naming the PR's old base, even for the refreshed head.
    case.payload_base = lambda row: case.old_base

    outcome = run(case)

    rounds = pushes(case)
    assert len(rounds) == 2, (
        f"The first stale report refreshes once; the same stale pair again must not refresh again "
        f"(heads pushed: {[row.head for row in rounds]})"
    )
    assert case.branch_pushes == 2, f"One publication and one refresh push, got {case.branch_pushes}"
    assert outcome == "held", f"Same stale bases after the refresh keep today's hold; got {outcome!r}"
    flag = case.store.load_flag(N)
    assert flag is not None and flag.reason == Reason.ACCEPTANCE_BASELINE_UNAVAILABLE
    assert not case.tracker.merged_prs
    assert not merge_owner.admissions(case.store), "A held run does not enter the merge queue"
    assert case.model_calls.get(N) == 1, "Stale CI evidence never costs a worker turn"


def test_missing_shards_keep_waiting_without_a_refresh(stale, monkeypatch):
    case = stale
    monkeypatch.setattr(case.tracker, "pr_acceptance_reports", lambda head: [])  # CI has uploaded nothing yet.

    outcome = run(case)

    assert len(pushes(case)) == 1, "Missing shards are not a base move: no refresh"
    assert outcome == "held" and case.store.load_flag(N).reason == Reason.ACCEPTANCE_BASELINE_UNAVAILABLE
    assert sum(case.sleeps) >= 1200 - 30, "Missing shards keep today's wait to the CI deadline"
