"""Acceptance test for conflicting-pr-rework: a PR that conflicts with its base goes to the conflict path.

When main moves with a change that conflicts with a worker's PR, GitHub builds no
merge ref for the PR and starts no pull_request CI: the PR shows zero checks and
no acceptance reports, and GitHub reports `mergeable: CONFLICTING`. Today the gate
waits for acceptance shards that never come ("missing acceptance shards:
report.shard_index: observed [], expected [...]"), holds the issue as
acceptance-baseline-unavailable at the CI deadline, and the orchestrator has to
notice the conflict and retry by hand (#1952 PR #1988 and #1491 PR #2024, 2026-11-04).

Now, while the gate waits for CI or acceptance reports on a PR head, a GitHub
answer of CONFLICTING stops the wait at once and sends the run to the existing
conflict path: merge the current base, let the worker resolve the conflict, push
the resolved head. CI then measures that head. UNKNOWN (GitHub still computing)
keeps today's wait, and a MERGEABLE PR whose shards are missing keeps today's
wait and hold.

Real runner, merge owner, Git, state and pytest measurements (the merge-owner
prototype world); only GitHub, models and the clock are fake. The fake GitHub
answers the PR's mergeable state with GitHub's own values (MERGEABLE,
CONFLICTING, UNKNOWN) through the fake tracker's `set_pr_mergeable(pr, state)`,
next to its existing `set_pr_checks`; the real tracker reads GitHub's
`mergeable` field.
"""

from datetime import timedelta
import subprocess

import pytest

from conftest import git_command
from foragent import merge_owner, runner
from foragent.executors import ExecResult
from foragent.reasons import Reason
from acceptance.test_merge_path_real_settings import world  # noqa: F401
from test_merge_owner_proto import proto  # noqa: F401
import test_merge_owner_proto as support

N = 5
BRANCH = f"foragent/issue-{N}"
OWN = f"src/member_{N}.py"
MAIN_TEXT = "VALUE = 'main owns this'\n"
RESOLVED = f"VALUE = {N}\n"
MEMORY = ".claude/agent-memory/fa-orch/MEMORY.new.md"
MEMORY_BASE = "- [id:base] earlier lesson\n"


def git(case, *args):
    return git_command(case.history.origin, *args)


def contains(case, ancestor, sha):
    return subprocess.run(["git", "merge-base", "--is-ancestor", ancestor, sha], cwd=case.history.origin,
                          capture_output=True, check=False).returncode == 0


def blob(case, sha, path):
    result = subprocess.run(["git", "rev-parse", "--verify", "-q", f"{sha}:{path}"], cwd=case.history.origin,
                            capture_output=True, text=True, check=False)
    return result.stdout.strip() or None


def conflicts_with_main(case, head):
    """What GitHub computes for a PR: a plain three-way text merge of `head` into main.

    GitHub runs no custom merge drivers, so a `merge=union` attribute that makes the
    local merge clean does not stop GitHub from reporting CONFLICTING.
    """
    origin = case.history.origin
    base = git(case, "merge-base", "main", head)
    ours = set(git(case, "diff", "--name-only", base, "main").splitlines())
    theirs = set(git(case, "diff", "--name-only", base, head).splitlines())
    scratch = case.scratch / "github-merge"
    scratch.mkdir(exist_ok=True)
    for path in sorted(ours & theirs):
        sides = [blob(case, sha, path) for sha in ("main", base, head)]
        if sides[0] == sides[2]:
            continue
        if None in sides:
            return True  # add/add or modify/delete
        files = []
        for name, sha in zip(("ours", "base", "theirs"), sides):
            target = scratch / name
            target.write_bytes(subprocess.run(["git", "cat-file", "blob", sha], cwd=origin,
                                              capture_output=True, check=True).stdout)
            files.append(str(target))
        if subprocess.run(["git", "merge-file", "-p", *files], capture_output=True, check=False).returncode:
            return True
    return False


def answer_mergeable(case):
    """The fake GitHub publishes its current mergeable answer for this issue's open PRs."""
    setter = getattr(case.tracker, "set_pr_mergeable", None)
    if setter is None:
        return  # Clean main has no mergeable read; the behaviour assertions decide the test.
    for pr in case.tracker.created_prs:
        if pr.head == BRANCH:
            setter(pr.number, case.mergeable(case.tracker.pr_head_sha(pr.number)))


@pytest.fixture
def conflicting(proto, monkeypatch):
    """Main gains a conflicting change at the moment the worker's PR branch is first pushed.

    `case.kind` picks the conflict: "real" (main adds the worker's own file with
    other contents) or "memory" (both sides append a lesson to a lane memory file
    that .gitattributes merges with merge=union, as in #1491). The run has already
    merged the base it fetched, so its pushed head conflicts with the new main on
    GitHub. GitHub then starts no CI for a conflicting head: no checks, no reports.
    A head that merges cleanly gets ordinary CI.
    """
    case = proto
    case.kind = "real"
    case.history.update({".gitattributes": ".claude/agent-memory/*/MEMORY.new.md merge=union\n",
                         MEMORY: MEMORY_BASE}, message="Lane memory merges by union")
    git_command(case.root, "fetch", "origin")
    git_command(case.root, "merge", "--ff-only", "origin/main")
    case.old_base = case.history.tip
    case.moved_to = None
    case.resolutions = 0
    case.resolved_at = None
    case.conflict_visible_after = 0.0  # seconds of waiting before GitHub stops answering UNKNOWN
    case.waited = 0.0
    factory = case.ctx.executor_factory

    def repairing_factory(name):
        executor = factory(name)
        if name != case.config.executor:
            return executor

        class Implementer:
            def run(self, **kwargs):
                if case.model_calls.get(N, 0) == 0:
                    result = executor.run(**kwargs)
                    memory = kwargs["worktree"] / MEMORY
                    memory.write_text(memory.read_text() + "- [id:worker] lesson from this run\n")
                    return result
                # Any later implementer turn is the conflict resolution of the moved base.
                case.model_calls[N] += 1
                case.resolutions += 1
                case.resolved_at = case.waited
                (kwargs["worktree"] / OWN).write_text(RESOLVED)
                return ExecResult(0, False, "Resolved the merge conflict", kwargs["log_path"])

        return Implementer()

    case.ctx.executor_factory = repairing_factory

    def mergeable(head):
        if conflicts_with_main(case, head):
            return "CONFLICTING" if case.waited >= case.conflict_visible_after else "UNKNOWN"
        return "MERGEABLE"

    case.mergeable = mergeable

    reports = case.ci.reports

    def measured(row):
        # GitHub runs no pull_request workflow for a head that conflicts with its base.
        return [] if row.branch == BRANCH and conflicts_with_main(case, row.head) else reports(row)

    monkeypatch.setattr(case.ci, "reports", measured)

    checks = case.tracker.pr_checks

    def pr_checks(pr):
        head = case.tracker.pr_head_sha(pr)
        row = case.ci.by_head.get(head)
        if row is not None and row.branch == BRANCH and not row.reports:
            return "none"  # Zero checks: CI never started.
        return checks(pr)

    monkeypatch.setattr(case.tracker, "pr_checks", pr_checks)

    transport = case.git._run
    case.branch_pushes = 0

    def moving_transport(argv):
        args = list(argv)
        while args[:1] == ["-C"]:
            args = args[2:]
        if args[:1] == ["push"] and args[-1].split(":")[-1].removeprefix("refs/heads/") == BRANCH:
            case.branch_pushes += 1
            if case.branch_pushes == 1:
                # Another lane merges a conflicting change just before the push lands.
                if case.kind == "real":
                    case.history.update({OWN: MAIN_TEXT}, message="Another lane owns this file")
                else:
                    case.history.update({MEMORY: MEMORY_BASE + "- [id:main] lesson from another run\n"},
                                        message="Foragent role memory: orchestrator")
                case.moved_to = case.history.tip
            if case.branch_pushes > 3:
                pytest.fail("The PR branch was pushed again and again: one conflict is resolved once")
        result = transport(argv)
        answer_mergeable(case)
        return result

    monkeypatch.setattr(case.git, "_run", moving_transport)

    create_pr = case.tracker.create_pr

    def created(*args, **kwargs):
        pr = create_pr(*args, **kwargs)
        answer_mergeable(case)
        return pr

    monkeypatch.setattr(case.tracker, "create_pr", created)
    case.sleeps = []

    def sleep(seconds):
        # The run's own waits advance the fake clock; nothing else sleeps.
        case.sleeps.append(seconds)
        case.waited += seconds
        case.clock[0] += timedelta(seconds=seconds)
        answer_mergeable(case)

    case.ctx.sleep = sleep
    return case


def run(case):
    case.clock[0] += timedelta(seconds=1)
    case.store.start_run(N, branch=BRANCH, worktree=case.store.worktree_path(N))
    case.tracker.add_labels(N, ["running"])
    case.worker.number = N
    return runner.run_issue(N, case.ctx)


def pushes(case):
    """Every head GitHub received on this issue's branch, in push order."""
    return [row for row in case.ci.by_head.values() if row.branch == BRANCH]


def log(case):
    path = case.store.log_path(N)
    return path.read_text() if path.exists() else ""


def test_conflicting_pr_goes_to_the_conflict_path_once_and_merges(conflicting):
    case = conflicting
    # GitHub answers UNKNOWN for the first five minutes of the wait, then CONFLICTING.
    case.conflict_visible_after = 300.0

    outcome = run(case)

    first = pushes(case)[0]
    assert case.moved_to is not None and first.base == case.moved_to and not first.reports, (
        "Setup: main moved with a conflicting change and GitHub started no CI for the PR head"
    )
    flag = case.store.load_flag(N)
    assert outcome == "merge-queued", (
        f"A conflicting PR must go to the conflict path, not wait for shards that never come; "
        f"got {outcome!r}, flag {flag}\n{log(case)[-2000:]}"
    )
    assert flag is None, f"No acceptance-baseline-unavailable hold for a conflicting PR: {flag}"
    assert case.resolutions == 1, f"The worker resolves the conflict exactly once, got {case.resolutions}"
    assert case.resolved_at >= case.conflict_visible_after, (
        f"UNKNOWN keeps today's wait: the run left after {case.resolved_at}s, "
        f"before GitHub reported CONFLICTING at {case.conflict_visible_after}s"
    )
    assert case.resolved_at < case.conflict_visible_after + 120, (
        f"CONFLICTING stops the wait at once; the run kept waiting until {case.resolved_at}s"
    )
    rounds = pushes(case)
    assert len(rounds) == 2 and case.branch_pushes == 2, (
        f"One publication and one resolved push; heads pushed: {[row.head for row in rounds]}"
    )
    resolved = rounds[1]
    assert contains(case, case.moved_to, resolved.head), "The resolution merges the current base into the PR"
    assert contains(case, first.head, resolved.head), "The resolution keeps the reviewed work"
    assert git(case, "show", f"{resolved.head}:{OWN}") == RESOLVED.strip(), "The pushed head carries the resolution"
    assert resolved.reports, "The resolved head merges cleanly, so CI measures it"
    assert any(head == resolved.head for _, head, _ in case.ci.reads), (
        "After the resolution the gate reads CI's reports for the resolved head"
    )

    support.drain(case, count=1)

    assert case.tracker.merged_prs == [support.pr_for(case, N).number], "The resolved PR merges once"
    assert case.merges[-1]["head"] == resolved.head
    assert len(pushes(case)) == 2, "Merging needs no further refresh"
    assert case.tracker.get_issue(N).state == "closed"


def test_resolved_but_unmeasured_pr_keeps_todays_wait_and_hold(conflicting, monkeypatch):
    case = conflicting
    # CONFLICTING from the start; after the resolution GitHub says MERGEABLE, but CI uploads no shards.
    monkeypatch.setattr(case.tracker, "pr_acceptance_reports", lambda head: [])

    outcome = run(case)

    rounds = pushes(case)
    assert case.resolutions == 1, (
        f"The conflicting PR reaches the conflict path exactly once (resolutions {case.resolutions}); "
        f"outcome {outcome!r}\n{log(case)[-2000:]}"
    )
    assert len(rounds) == 2 and case.branch_pushes == 2, (
        f"One publication and one resolved push, no further refresh; heads pushed: {[row.head for row in rounds]}"
    )
    resolved = rounds[1]
    assert contains(case, case.moved_to, resolved.head) and not conflicts_with_main(case, resolved.head)
    assert case.resolved_at < 1200 - 30, "The conflict ended the first wait early"
    assert case.waited - case.resolved_at >= 1200 - 30, (
        "A MERGEABLE head with missing shards keeps today's wait to the CI deadline"
    )
    assert outcome == "held", f"Missing shards on a MERGEABLE head keep today's hold; got {outcome!r}"
    flag = case.store.load_flag(N)
    assert flag is not None and flag.reason == Reason.ACCEPTANCE_BASELINE_UNAVAILABLE
    assert not case.tracker.merged_prs
    assert not merge_owner.admissions(case.store), "A held run does not enter the merge queue"


def test_memory_only_conflict_is_refreshed_without_a_worker_turn(conflicting):
    case = conflicting
    case.kind = "memory"

    outcome = run(case)

    first = pushes(case)[0]
    assert case.moved_to is not None and first.base == case.moved_to and not first.reports, (
        "Setup: main appended to the union-merged memory file and GitHub started no CI for the PR head"
    )
    flag = case.store.load_flag(N)
    assert outcome == "merge-queued", (
        f"A memory-only conflict must refresh the PR, not wait for shards that never come; "
        f"got {outcome!r}, flag {flag}\n{log(case)[-2000:]}"
    )
    assert flag is None
    assert case.resolutions == 0 and case.model_calls.get(N) == 1, (
        "The union merge resolves a memory-only conflict: no worker turn"
    )
    assert case.waited < 600, f"CONFLICTING stops the wait at once; waited {case.waited}s"
    rounds = pushes(case)
    assert len(rounds) == 2 and case.branch_pushes == 2, (
        f"One publication and one refreshed push; heads pushed: {[row.head for row in rounds]}"
    )
    refreshed = rounds[1]
    assert contains(case, case.moved_to, refreshed.head), "The refresh merges the current base into the PR"
    assert contains(case, first.head, refreshed.head), "The refresh keeps the reviewed work"
    memory = git(case, "show", f"{refreshed.head}:{MEMORY}")
    assert "[id:main]" in memory and "[id:worker]" in memory, "Both lessons survive the union merge"
    assert refreshed.reports, "Main is now in the PR head, so GitHub runs CI on it"

    support.drain(case, count=1)

    assert case.tracker.merged_prs == [support.pr_for(case, N).number], "The refreshed PR merges once"
    assert case.merges[-1]["head"] == refreshed.head
    assert case.tracker.get_issue(N).state == "closed"
