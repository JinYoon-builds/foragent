"""Acceptance test for behind-workflow-no-local-baseline (architect r1018, 2026-11-07).

A PR is trusted to use CI artifacts only when its acceptance workflow matches main.
Today that check diffs main's trusted commit against the PR head with two dots, so
a PR whose head was pushed before main edited .github/workflows/foragent-acceptance*.yml
counts as "changes the workflow" and always gets a full local acceptance baseline on
the base (60 minutes and a hold for #2449 and #2172 on 11-07).

Now:
- A PR whose head only lacks a later acceptance-workflow edit on main, and which
  changes no workflow file since its merge base, is updated onto main, waits for CI
  on the new head, and starts no local full acceptance baseline.

Artifacts produced by an older workflow are never read (already true on main and
guarded by existing tests); the behind-only test below only checks that the update
does not change that, as a side condition of the update path.

Real runner, merge owner, Git, state and pytest measurements (the merge-owner
prototype world); only GitHub, models and the clock are fake. Everything is observed
at public boundaries: the test processes the runner starts (`runner.run_logged`,
wrapped and passed through unchanged), Git pushes, CI artifact reads, run history
and the actual merge into main.
"""

from datetime import timedelta
from pathlib import Path
import shlex
import subprocess

import pytest

from conftest import git_command
from foragent import runner
from acceptance.test_merge_path_real_settings import world  # noqa: F401
from test_merge_owner_proto import proto  # noqa: F401
import test_merge_owner_proto as support

N = 5
BRANCH = f"foragent/issue-{N}"
WORKFLOW = ".github/workflows/foragent-acceptance.yml"


def git(case, *args):
    return git_command(case.history.origin, *args)


def contains(case, ancestor, sha):
    return subprocess.run(["git", "merge-base", "--is-ancestor", ancestor, sha], cwd=case.history.origin,
                          capture_output=True, check=False).returncode == 0


def workflow_text(case, sha, path=WORKFLOW):
    result = subprocess.run(["git", "show", f"{sha}:{path}"], cwd=case.history.origin,
                            capture_output=True, text=True, check=False)
    return result.stdout if result.returncode == 0 else None


@pytest.fixture
def behind(proto, monkeypatch):
    """Main gains an acceptance-workflow edit right after CI measured the first PR push.

    `case.pr_workflow` (None, or {path: text}) makes the worker's own change also
    edit an acceptance workflow file. The edit on main never conflicts with the PR.
    """
    case = proto
    case.pr_workflow = None
    case.main_workflow = workflow_text(case, case.history.tip) + "# producer v2\n"
    case.old_base = case.history.tip
    case.moved_to = None
    case.branch_pushes = 0

    factory = case.ctx.executor_factory

    def worker_factory(name):
        executor = factory(name)
        if name != case.config.executor:
            return executor

        class Implementer:
            def run(self, **kwargs):
                result = executor.run(**kwargs)
                for path, text in (case.pr_workflow or {}).items():
                    target = kwargs["worktree"] / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(text)
                return result

        return Implementer()

    case.ctx.executor_factory = worker_factory

    transport = case.git._run

    def moving_transport(argv):
        args = list(argv)
        while args[:1] == ["-C"]:
            args = args[2:]
        pushing = args[:1] == ["push"] and args[-1].split(":")[-1].removeprefix("refs/heads/") == BRANCH
        if pushing:
            case.branch_pushes += 1
            if case.branch_pushes > 3:
                pytest.fail("The PR branch was pushed again and again: one update onto main is enough")
        result = transport(argv)
        if pushing and case.branch_pushes == 1:
            # CI has measured this head with its own (old) acceptance workflow. Right after,
            # another lane merges an acceptance-workflow edit to main, and the run's
            # checkout already knows the new main (any fetch would bring it).
            case.history.update({WORKFLOW: case.main_workflow}, message="Edit the acceptance workflow")
            case.moved_to = case.history.tip
            git_command(case.root, "fetch", "origin")
        return result

    monkeypatch.setattr(case.git, "_run", moving_transport)

    run_logged = runner.run_logged

    def observed_process(argv, *args, cwd=None, **kwargs):
        # Pass-through: the real test process runs; only its command and checkout are noted.
        command = argv[2] if list(argv[:2]) == ["/bin/sh", "-c"] else shlex.join(argv)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True,
                              check=False).stdout.strip() if cwd else ""
        case.processes.append((command, Path(cwd) if cwd else None, head, case.moved_to))
        return run_logged(argv, *args, cwd=cwd, **kwargs)

    case.processes = []
    monkeypatch.setattr(runner, "run_logged", observed_process)

    case.sleeps = []

    def sleep(seconds):
        # The run's own waits advance the fake clock; nothing else sleeps.
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
    """Every head GitHub received on this issue's branch, in push order."""
    return [row for row in case.ci.by_head.values() if row.branch == BRANCH]


def reads_of(case, head):
    return [read for read in case.ci.reads if read[1] == head]


def log(case):
    path = case.store.log_path(N)
    return path.read_text() if path.exists() else ""


def full_baselines_after_move(case):
    """Test processes started after main's workflow edit that run the whole acceptance suite.

    A full local acceptance baseline selects the acceptance directory as a whole (not
    single files or node ids) in a checkout other than the PR's own worktree.
    """
    directory = case.config.acceptance_dir.rstrip("/")
    own = case.store.worktree_path(N)
    found = []
    for command, cwd, head, moved in case.processes:
        words = [word.rstrip("/") for word in shlex.split(command)]
        if moved is not None and "pytest" in command and directory in words and cwd != own:
            found.append((command, head))
    return found


def assert_setup(case):
    first = pushes(case)[0]
    assert case.moved_to is not None and first.base != case.moved_to and contains(case, first.base, case.moved_to), (
        "Setup: main edited the acceptance workflow after CI measured the first PR head"
    )
    assert workflow_text(case, first.head) != workflow_text(case, case.moved_to), (
        "Setup: the first PR head lacks main's acceptance-workflow edit"
    )
    return first


def test_behind_only_pr_is_updated_onto_main_without_a_local_baseline(behind):
    case = behind

    outcome = run(case)

    first = assert_setup(case)
    assert not full_baselines_after_move(case), (
        f"A PR only behind a workflow edit starts no local full acceptance baseline; "
        f"processes {full_baselines_after_move(case)}\n{log(case)[-2000:]}"
    )
    flag = case.store.load_flag(N)
    assert outcome == "merge-queued" and flag is None, (
        f"The updated PR waits for CI and joins the merge queue; got {outcome!r}, flag {flag}\n{log(case)[-2000:]}"
    )
    rounds = pushes(case)
    assert len(rounds) == 2 and case.branch_pushes == 2, (
        f"One publication and exactly one update onto main; heads pushed: {[row.head for row in rounds]}"
    )
    updated = rounds[1]
    assert contains(case, case.moved_to, updated.head), "The update merges main (with its workflow edit) into the PR"
    assert contains(case, first.head, updated.head), "The update keeps the reviewed work"
    assert workflow_text(case, updated.head) == case.main_workflow, (
        "The updated head carries main's acceptance workflow"
    )
    assert updated.reports, "CI measures the updated head"
    assert reads_of(case, updated.head), "After the update the gate reads CI's reports for the updated head"
    assert not reads_of(case, first.head), "The pre-update head, measured by the older workflow, is not evidence"

    support.drain(case, count=1)

    assert case.tracker.merged_prs == [support.pr_for(case, N).number], "The updated PR merges once"
    assert case.merges[-1]["head"] == updated.head
    assert not full_baselines_after_move(case), "Merging the updated PR needs no local full baseline either"
    comparison = case.store.get_run(N).comparison
    assert comparison and comparison.get("source") == "ci", (
        f"Run history records CI (not a local comparison) as the evidence: {comparison}"
    )
    assert case.tracker.get_issue(N).state == "closed"

