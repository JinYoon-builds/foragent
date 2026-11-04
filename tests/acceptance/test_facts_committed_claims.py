"""Acceptance test for facts-committed-claims. Locked by Foragent; do not edit.

`fa orch facts` replays the gate's start loop. A retried issue whose branch already
has commits is measured by the gate on its committed files, not its declared glob, so
the replay must not report a file-overlap hold that the gate does not apply. The
fixture follows the 2026-11-04 14:36Z state: one running run on messages/orch_stop.py,
four ready retried issues declaring src/foragent/messages/** with committed files on
their branches, and one issue on a file three running runs already hold. A second
group on src/foragent/notify/** keeps the other side locked: a start whose branch has no
commit yet still claims its declared glob, as the gate does, and so fills the group.
"""

from dataclasses import replace
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess

import pytest

from conftest import ACCEPTANCE, git_command
from foragent import capacity, cli, resource_sample, scheduler
from foragent.issue import body_hash
from foragent.state import StateStore
from test_scheduler import BODY, NOW, approve, ctx  # noqa: F401
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401
from test_state_split import offline_acceptance  # noqa: F401


MESSAGES = "src/foragent/messages/**"
SHARED = "src/foragent/run_publish_reviewed.py"
RUNNING_MESSAGES = 1491
RETRIED = {  # Issue -> files its existing branch has committed.
    1500: ["src/foragent/messages/decisions.py"],
    1816: ["src/foragent/messages/presence.py"],
    1819: ["src/foragent/messages/settings.py", "src/foragent/watch.py"],
    1954: ["src/foragent/messages/pm_fresh.py"],
}
SHARED_PEERS = (1600, 1601, 1602)
SHARED_WAITER = 1514
NOTIFY = "src/foragent/notify/**"
RUNNING_NOTIFY = 2000  # Running, measured on notify/push.py.
UNCOMMITTED = 2001  # An earlier execution and a branch, but no commit yet: claims notify/**.
FRESH = 2002  # Never ran: held, the group {notify/push.py, notify/**} is full.
COMMITTED_NOTIFY = 2003  # Committed notify/mail.py, yet its own declared glob meets the full group.


@pytest.fixture(autouse=True)
def offline_gate(tmp_path, monkeypatch, isolated_home, offline_acceptance):
    """Only fixture-local Git may spawn; process observations come from ctx's fake table."""
    popen = subprocess.Popen

    def local_git(argv, *args, **kwargs):
        assert isinstance(argv, (list, tuple)) and argv[0] == "git", argv
        assert Path(kwargs["cwd"]).resolve().is_relative_to(tmp_path.resolve()), kwargs
        return popen(argv, *args, **kwargs)

    def no_signal(*args, **kwargs):
        pytest.fail("These tests must never signal real processes")

    monkeypatch.setattr(subprocess, "Popen", local_git)
    monkeypatch.setattr(os, "kill", no_signal)
    monkeypatch.setattr(os, "killpg", no_signal)


@pytest.fixture
def case(ctx, monkeypatch):  # noqa: F811
    ctx.config = replace(
        ctx.config, repo=ctx.repo, max_parallel=None, overlap_per_file=2, overlap_mode="fixed",
        skill_libraries=[], test_command="true", language="en",
        lanes={lane: {"paths": ["src/foragent/**"]} for lane in ("view", "orch", "board")},
    )
    ctx.start_runs = True
    ctx.launched = []

    def launch(number):
        ctx.launched.append(number)
        return 7000 + number  # The shared ctx fixture records fake birth evidence.

    ctx.launcher = launch
    monkeypatch.setattr(cli, "load_config", lambda root: ctx.config)
    monkeypatch.setattr(StateStore, "for_repo", classmethod(
        lambda cls, repo, **kwargs: cls(ctx.store.root, now=ctx.now)))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr(cli, "datetime", Clock)
    capacity.write_state(ctx.store, 16, NOW)  # Free machine slots for every waiting issue.
    resource_sample.record(ctx.store, ("normal", 0.1), NOW)  # Facts read this, not the live host.
    ctx.monkeypatch = monkeypatch
    return ctx


def _issue(case, number, files, *, lane, running=False, committed=None, branch_commits=None):
    """Approve an issue; a running one gets a recorded measurement, a waiting one a retried branch.

    ``committed``: the files its last measurement recorded on the branch (None: no commits yet).
    ``branch_commits``: files to commit on its existing branch worktree (None: no worktree,
    []: a worktree on its branch with no commit yet).
    """
    body = BODY + "\n## Files\n" + "".join(f"- {path}\n" for path in files)
    labels = [f"lane:{lane}", "approved", "running" if running else "ready"]
    approve(case, number, body=body, labels=[f"lane:{lane}", "running" if running else "ready"])
    case.store.save_approval(
        number, body=body, body_sha=body_hash(body),
        acceptance_blobs=case.git.blob_shas("origin/main", [ACCEPTANCE]),
        approved_by="human", approved_at=case.now().isoformat(),
    )
    case.store.update_local_issue(number, title=f"Issue {number}", labels=labels, state="open", body=body)
    if branch_commits is not None:
        worktree = case.store.worktree_path(number)
        git_command(case.repo_root, "worktree", "add", "-b", f"foragent/issue-{number}", str(worktree), "origin/main")
        for path in branch_commits:
            target = worktree / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"# change for #{number}\n", encoding="utf-8")
        if branch_commits:
            # Plumbing only: the shared guard forbids porcelain commits in fixtures.
            git_command(worktree, "add", "-A")
            tree = git_command(worktree, "write-tree")
            commit = git_command(worktree, "-c", "user.name=Tests", "-c", "user.email=tests@example.com",
                                 "commit-tree", tree, "-p", "HEAD", "-m", f"Work on #{number}")
            git_command(worktree, "update-ref", f"refs/heads/foragent/issue-{number}", commit)
    if running:
        case.store.start_run(number, branch=f"foragent/issue-{number}",
                             worktree=case.store.worktree_path(number), lane=lane, pid=7000 + number)
        case.store.update_run(number, declared_files=files, changed_files=committed,
                              effective_files=committed, files_committed=True)
    elif committed is not None or branch_commits is not None:
        # The earlier execution of this issue, finished; its last measurement stays recorded.
        case.store.start_run(number, branch=f"foragent/issue-{number}",
                             worktree=case.store.worktree_path(number), lane=lane)
        case.store.update_run(number, declared_files=files, changed_files=committed or [],
                              effective_files=committed if committed is not None else files,
                              files_committed=committed is not None)
        case.store.finish_run(number, outcome="failed", detail="review rounds used up")


def _state_at_1436(case):
    _issue(case, RUNNING_MESSAGES, [MESSAGES], lane="view", running=True,
           committed=["src/foragent/messages/orch_stop.py"])
    for number in SHARED_PEERS:
        _issue(case, number, [SHARED], lane="orch", running=True, committed=[SHARED])
    for number, files in sorted(RETRIED.items()):
        _issue(case, number, [MESSAGES], lane="view", committed=files, branch_commits=files)
    _issue(case, SHARED_WAITER, [SHARED], lane="orch")
    _issue(case, RUNNING_NOTIFY, [NOTIFY], lane="board", running=True, committed=["src/foragent/notify/push.py"])
    _issue(case, UNCOMMITTED, [NOTIFY], lane="board", branch_commits=[])
    _issue(case, FRESH, [NOTIFY], lane="board")
    mail = ["src/foragent/notify/mail.py"]
    _issue(case, COMMITTED_NOTIFY, [NOTIFY], lane="board", committed=mail, branch_commits=mail)


HELD = {SHARED_WAITER, FRESH, COMMITTED_NOTIFY}
STARTED = {*RETRIED, UNCOMMITTED}


def _facts(case, capsys, *, human=False):
    """`fa orch facts` under the facts no-process rule: it may not spawn or signal anything."""
    def forbidden(*args, **kwargs):
        pytest.fail("fa orch facts must only read local records, never launch or signal a process")

    capsys.readouterr()
    with case.monkeypatch.context() as patch:
        patch.setattr(subprocess, "Popen", forbidden)
        code = cli_exit(cli.main, ["orch", "facts", "--repo-root", str(case.repo_root)]
                        + ([] if human else ["--json"]))
    output = capsys.readouterr()
    assert code == 0, f"fa orch facts failed: {output.err.strip()}"
    return output.out if human else json.loads(output.out)


def _held(result):
    return result["resources"]["parallelizable_work"]["held"]


def test_retried_issues_with_committed_branches_are_not_held_by_their_declared_glob(case, capsys):
    _state_at_1436(case)

    result = _facts(case, capsys)
    held = _held(result)
    work = result["resources"]["parallelizable_work"]
    assert set(work["waiting_issues"]) == HELD | STARTED
    assert not {str(number) for number in RETRIED} & held.keys(), (
        "A retried issue's simulated start claims its committed files, as the gate measures them; "
        f"its declared src/foragent/messages/** must not fill a file group: held={held}")
    assert held == {str(number): "structure" for number in sorted(HELD)}, (
        f"#{SHARED_WAITER}'s file is already held by three measured runs, and #{UNCOMMITTED}'s start "
        f"without commits claims {NOTIFY}, which fills the group for #{FRESH} and #{COMMITTED_NOTIFY}: held={held}")
    binding = result["binding"]
    assert binding["kind"] == "structure" and f"#{SHARED_WAITER}" in binding["detail"]
    assert not any(f"#{number}" in binding["detail"] for number in STARTED), binding
    human = _facts(case, capsys, human=True)
    holds = [line for line in human.splitlines() if "structure" in line]
    assert holds and any(f"#{SHARED_WAITER}" in line for line in holds), human
    assert not any(f"#{number}" in line for line in holds for number in STARTED), (
        f"The human view names the same holds as --json: {holds}")


def test_facts_holds_are_the_holds_the_gate_applies_on_the_same_state(case, capsys):
    _state_at_1436(case)
    result = _facts(case, capsys)
    held = {int(number) for number in _held(result)}
    waiting = set(result["resources"]["parallelizable_work"]["waiting_issues"])

    report = scheduler.tick(case)

    assert report.skipped is None, report
    started = set(case.launched)
    assert started == waiting - held, (
        f"Facts held {sorted(held)} of {sorted(waiting)}, but the gate started {sorted(started)} "
        f"on the same state: {report.actions}")
    assert started == STARTED, report.actions
    assert case.process_table.signals == []
