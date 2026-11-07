"""A queued PR that conflicts with main, for unit tests of its follow-up (copied from the locked rework test)."""

from datetime import timedelta
import subprocess
import pytest
from conftest import git_command
from foragent import runner
from foragent.executors import ExecResult


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
