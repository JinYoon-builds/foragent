"""Main writes cannot invalidate a successful final check before its merge.

Exercise public runner/order/role entry points. GitHub is FakeTracker; the
existing Git transport imports objects instead of running commit/push. Only
the Git/OS boundaries schedule the contender. No production helper or lock
name is imported, replaced or asserted. A no-op publisher, skipped merge or
duplicate effect fails the completion/content assertions too.
"""

from dataclasses import replace
import fcntl
import socket
from threading import Event, Thread, local

import pytest

from conftest import git_command
from runner_support import run_case  # noqa: F401
from test_orch import review_case  # noqa: F401
from foragent import orch, order, order_flush, order_notes, roles_commands, runner
from foragent.git import GitRepo
from foragent.state import StateStore


def incoming_plan():
    return {
        "id": "incoming-main-write", "title": "Incoming order", "summary": "A concurrent order.",
        "spec_markdown": "# Incoming order\n", "new_lanes": [], "edits": [],
        "issues": [{"key": "incoming", "title": "Incoming work", "goal": "Complete incoming work.",
                    "out_of_scope": "Other work.", "done_when": ["true"], "checklist": [],
                    "lane": "main", "size": "s", "blocked_by": [], "acceptance_files": {}}],
    }


def writer_for(case, kind):
    root, git, config = case.ctx.repo_root, case.ctx.git, case.ctx.config
    store = StateStore.for_repo(case.ctx.repo)
    plan = incoming_plan()

    def publish(**kwargs):
        return order.publish(plan, root, config, case.tracker, repo=case.ctx.repo, git=git, **kwargs)

    if kind == "order-flush":
        assert publish(defer="boundary").waiting_for == "boundary"
    elif kind == "note-flush":
        order_notes.append(store, kind="decision", boundary="boundary", content="Retained decision")
    elif kind == "role-memory":
        (root / ".foragent.yml").write_text(
            "repo: owner/repo\nroles:\n  archivist:\n    agent: fa-archivist\n"
            "    skills: []\n    signals: []\n    memory: .claude/agent-memory/fa-archivist/\n"
        )
        memory = root / ".claude/agent-memory/fa-archivist/MEMORY.md"
        memory.parent.mkdir(parents=True)
        memory.write_text("A reusable role lesson.\n")

    def write():
        if kind == "order-publish":
            publish()
        elif kind in {"order-flush", "note-flush"}:
            result = order_flush.flush(root, config, case.tracker, git, store,
                                       repo=case.ctx.repo, boundary="boundary")
            assert result["status"] == "published", result
        else:
            roles_commands.set_role(root, "archivist", why="Retain role memory", git=git)

    def assert_written():
        if kind.startswith("order-"):
            assert [i.title for i in case.tracker.list_issues()].count("Incoming work") == 1
            assert git_command(case.history.origin, "show", "main:docs/specs/incoming-main-write.md") == "# Incoming order"
        elif kind == "note-flush":
            assert git_command(case.history.origin, "show", "main:docs/decisions.md").count("Retained decision") == 1
        else:
            assert git_command(root, "show", "main:.claude/agent-memory/fa-archivist/MEMORY.md") == "A reusable role lesson."

    return write, assert_written


def race_after_check(case, kind, entry, monkeypatch, *, contended=True):
    """Let the writer run until completed or demonstrably waiting in the OS.

    A nonblocking flock probe observes real contention without depending on
    the lock path, its owner, or an elapsed-time guess. The ordinary blocking
    call is preserved. All worker exceptions are returned to the test.
    """
    case.ctx.config = replace(case.ctx.config, repo="owner/repo", merge_group_max=1, test_command="true")
    write, assert_written = writer_for(case, kind)
    settled, finished = Event(), Event()
    worker = local()
    failures, checks, merges, invalid = [], [], [], []
    started = False
    real_flock = fcntl.flock

    def flock(fd, operation):
        if getattr(worker, "writer", False) and operation & fcntl.LOCK_EX:
            try:
                return real_flock(fd, operation | fcntl.LOCK_NB)
            except BlockingIOError:
                settled.set()
                if operation & fcntl.LOCK_NB:
                    raise
        return real_flock(fd, operation)

    def execute():
        worker.writer = True
        try:
            write()
        except BaseException as exc:
            failures.append(exc)
        finally:
            finished.set()
            settled.set()

    thread = Thread(target=execute, daemon=True)

    def arrive():
        nonlocal started
        if not started:
            started = True
            thread.start()
            assert settled.wait(10), "Incoming main write neither finished nor waited at an OS lock"

    check = GitRepo.check_publication

    def checked(git, base):
        result = check(git, base)
        if not getattr(worker, "writer", False):
            # Ref values are public Git results, independent of the chosen lock.
            checks.append((base, git_command(case.ctx.repo_root, "rev-parse", "refs/heads/main")))
            run = case.store.get_run(1)
            if contended and entry != "human" and run is not None and run.phase == "merging":
                arrive()  # After the successful final check, before returning to the merger.
        return result

    merge = case.tracker.merge_pr

    def github_merge(pr, *, expected_head_sha, **kwargs):
        assert checks, "A successful publication check must precede a merge"
        if contended:
            arrive()  # Human merge: pause at the external API boundary after all its checks.
        base = git_command(case.history.origin, "rev-parse", "refs/heads/main")
        local_main = git_command(case.ctx.repo_root, "rev-parse", "refs/heads/main")
        if (base, local_main) != checks[-1]:
            invalid.append({"checked": checks[-1], "merged_on": (base, local_main)})
        merge(pr, expected_head_sha=expected_head_sha, **kwargs)
        # Emulate GitHub's squash on the actual remote base, retaining both trees.
        tree = git_command(case.history.origin, "merge-tree", "--write-tree", base, expected_head_sha).splitlines()[0]
        sha = git_command(case.history.origin, "-c", "user.name=Offline GitHub", "-c",
                          "user.email=offline@example.com", "commit-tree", tree, "-p", base,
                          input="Offline squash\n")
        git_command(case.history.origin, "update-ref", "refs/heads/main", sha, base)
        case.history.tip = sha
        merges.append((pr, base, finished.is_set()))

    def no_network(*args, **kwargs):
        pytest.fail("This lock must use the fake GitHub transport")

    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(fcntl, "flock", flock)
    monkeypatch.setattr(GitRepo, "check_publication", checked)
    monkeypatch.setattr(case.tracker, "merge_pr", github_merge)
    try:
        if entry == "implementation":
            outcome = runner.run_issue(1, case.ctx)
        elif entry == "reviewed":
            outcome = runner.review_only(1, case.ctx)
        else:
            try:
                outcome = orch.merge(case.ctx, case.pr, approved_by="jin")
            except orch.OrchError as exc:
                outcome = str(exc)  # A moved-base refusal is a permitted safe outcome.
        if not contended:
            arrive()
    finally:
        if started:
            thread.join(10)
    assert started, "The final-check race was not exercised"
    assert finished.is_set(), "The main writer remained stuck after the merge ended"
    assert not failures, failures
    assert not invalid, f"A merge landed on an unverified base: {invalid}"
    assert_written()
    if not merges and contended:
        assert case.tracker.merged_prs == [] and case.tracker.get_issue(1).state == "open"
        assert checks[-1] != (
            git_command(case.history.origin, "rev-parse", "refs/heads/main"),
            git_command(case.ctx.repo_root, "rev-parse", "refs/heads/main"),
        ), "A stopped merge must have a moved base in this schedule"
    else:
        assert outcome == "merged" or (entry == "human" and "merged" in outcome), outcome
        assert len(merges) == len(case.tracker.merged_prs) == 1
        assert git_command(case.history.origin, "show", "main:feature.txt") == "done"


@pytest.mark.parametrize("kind", ["order-publish", "order-flush", "note-flush", "role-memory"])
def test_main_writer_arriving_after_final_check_cannot_invalidate_merge(run_case, monkeypatch, kind):
    race_after_check(run_case, kind, "implementation", monkeypatch)


@pytest.mark.parametrize("entry", ["reviewed", "human"])
def test_recovered_and_human_merges_exclude_incoming_publication(review_case, monkeypatch, entry):
    race_after_check(review_case, "order-publish", entry, monkeypatch)


def test_uncontended_implementation_merge_and_publication_complete(run_case, monkeypatch):
    race_after_check(run_case, "order-publish", "implementation", monkeypatch, contended=False)


@pytest.mark.parametrize("entry", ["reviewed", "human"])
def test_uncontended_recovered_and_human_merges_and_publication_complete(review_case, monkeypatch, entry):
    race_after_check(review_case, "order-publish", entry, monkeypatch, contended=False)
