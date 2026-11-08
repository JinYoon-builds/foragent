"""Shared world for full-suite-baseline-to-ci (2026-11-08); a helper, not a test module.

A lane run whose changed files select the whole acceptance suite: the worker's own
change edits the acceptance CI workflow (.github/workflows/foragent-acceptance.yml),
as #2768 did. Everything else is the merge-owner prototype world: real runner,
merge owner, Git, state and pytest measurements; only GitHub, models and the clock
are fake. Test processes the runner starts are observed by wrapping
`runner.run_logged` and passing every call through unchanged.
"""

from datetime import timedelta
from pathlib import Path
import re
import shlex
import subprocess

from conftest import git_command
from foragent import runner

N = 5
BRANCH = f"foragent/issue-{N}"
WORKFLOW = ".github/workflows/foragent-acceptance.yml"


def workflow_text(case, sha, path=WORKFLOW):
    result = subprocess.run(["git", "show", f"{sha}:{path}"], cwd=case.history.origin,
                            capture_output=True, text=True, check=False)
    return result.stdout if result.returncode == 0 else None


def prepare(case, monkeypatch, *, edit_workflow=True):
    """The worker's change also edits the acceptance workflow (when ``edit_workflow``)."""
    case.pr_workflow = ({WORKFLOW: (workflow_text(case, case.history.tip) or "") + "# lane edit\n"}
                        if edit_workflow else {})
    factory = case.ctx.executor_factory

    def worker_factory(name):
        executor = factory(name)
        if name != case.config.executor:
            return executor

        class Implementer:
            def run(self, **kwargs):
                result = executor.run(**kwargs)
                for path, text in case.pr_workflow.items():
                    target = kwargs["worktree"] / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(text)
                return result

        return Implementer()

    case.ctx.executor_factory = worker_factory

    run_logged = runner.run_logged
    case.processes = []

    def observed_process(argv, *args, cwd=None, **kwargs):
        # Pass-through: the real test process runs; only its command and checkout are noted.
        command = argv[2] if list(argv[:2]) == ["/bin/sh", "-c"] else shlex.join(argv)
        case.processes.append((command, Path(cwd) if cwd else None))
        return run_logged(argv, *args, cwd=cwd, **kwargs)

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


def log(case):
    path = case.store.log_path(N)
    return path.read_text(errors="replace") if path.exists() else ""


def full_baselines(case):
    """Test processes that measure the whole acceptance suite locally.

    A full local acceptance baseline selects the acceptance directory as a whole
    (not single files or node ids). The local_only marker capture (`-m local_only`)
    is a different, small measurement and does not count.
    """
    directory = case.config.acceptance_dir.rstrip("/")
    found = []
    for command, cwd in case.processes:
        words = [word.rstrip("/") for word in shlex.split(command)]
        if "pytest" in command and directory in words and "local_only" not in words:
            found.append((command, cwd))
    return found


def pr_opened(case):
    return [pr for pr in case.tracker.created_prs if pr.head == BRANCH]


LEFT_TO_CI = re.compile(r"acceptance.*(PR CI|pull request CI)|(PR CI|pull request CI).*acceptance", re.I)


def left_to_ci_records(case):
    """Every local record that says the acceptance comparison is left to PR CI.

    The run log, the orchestrator journal and the run record are the run's local
    records; the wording only needs to name the acceptance comparison and PR CI.
    """
    found = [line for line in log(case).splitlines() if LEFT_TO_CI.search(line)]
    journal = case.store.root / "orch-events.jsonl"
    if journal.exists():
        found += [line for line in journal.read_text(errors="replace").splitlines()
                  if f'"issue": {N}' in line and LEFT_TO_CI.search(line)]
    run_record = case.store.get_run(N)
    for field in ("comparison", "detail"):
        value = getattr(run_record, field, None)
        if value and LEFT_TO_CI.search(str(value)):
            found.append(f"run.{field}: {value}")
    return found


def assert_setup(case):
    pushed = [row for row in case.ci.by_head.values() if row.branch == BRANCH]
    assert pushed, f"Setup: the run pushed its branch\n{log(case)[-2000:]}"
    head = pushed[-1].head
    base = git_command(case.history.origin, "merge-base", head, case.history.tip)
    changed = git_command(case.history.origin, "diff", "--name-only", f"{base}...{head}").split()
    assert WORKFLOW in changed, f"Setup: the PR's own change edits the acceptance workflow; changed {changed}"
    return head

