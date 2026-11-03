"""A saved branch is merged onto the approved base before its locks are compared (#1679).

After the merge, any locked file that differs from approval (or conflicts) is the branch's own edit.
"""

from dataclasses import replace
from datetime import timedelta

import pytest

from conftest import git_command
from runner_support import run_case  # noqa: F401
from foragent.issue import body_hash
from foragent.reasons import Reason
from foragent.runner import run_issue

BRANCH = "foragent/issue-1"
FIRST, SECOND = "tests/acceptance/test_first_lock.py", "tests/acceptance/test_second_lock.py"
OLD, MID, NEW = "# old\n", "# mid\n", "# new\n"


def commit(case, parent, files, *, merge=None):
    """Write the issue branch's own commit with plumbing; a value of None deletes the path."""
    stream = [f"commit refs/heads/{BRANCH}\n", "committer Tests <t@example.com> 1792483200 +0000\n",
              f"data 10\nIssue work\nfrom {parent}\n", *([f"merge {merge}\n"] if merge else [])]
    for path, content in files.items():
        stream.append(f"D {path}\n" if content is None
                      else f"M 100644 inline {path}\ndata {len(content.encode())}\n{content}\n")
    git_command(case.history.origin, "fast-import", "--quiet", input="".join(stream) + "\ndone\n")
    return git_command(case.history.origin, "rev-parse", BRANCH)


def configure(case):
    case.ctx.config = replace(case.ctx.config, review="fast", max_retries=0, skill_libraries=[],
                              test_command="true", checks="local", approvers=["human"])


def approve(case):
    body = ("<!-- foragent:v1 -->\n## Goal\nWork.\n## Out of scope\nNone.\n## Done when\n```sh\n"
            f"test -f {SECOND}\n```\n```sh\ntest -f src/x.py || test -f {FIRST}\n```\n")
    case.ctx.git.fetch("origin", "main")
    case.tracker.update_issue(1, title="Work", body=body)
    case.store.save_approval(1, body=body, body_sha=body_hash(body),
                             acceptance_blobs=case.ctx.git.blob_shas("origin/main", [FIRST, SECOND]),
                             approved_by="human", approved_at=case.now[0].isoformat())


def run_saved(case, own, *, main_after, merge_old_main=False):
    configure(case)
    case.history.update({FIRST: OLD, SECOND: OLD, "src/x.py": "X = 0\n"})
    old_main = case.history.tip
    case.history.update({FIRST: MID})
    head = commit(case, case.history.tip, own, merge=old_main if merge_old_main else None)
    case.history.update(main_after)
    approve(case)
    case.executor.steps = [{".foragent/QUESTION.md": "Stopped after implementation started."}]
    return run_issue(1, case.ctx), head


@pytest.mark.parametrize("own", [{FIRST: OLD}, {FIRST: None}], ids=["rollback", "deletion"])
def test_own_lock_edit_survives_the_base_merge_and_invalidates(run_case, own):
    outcome, head = run_saved(run_case, own, main_after={SECOND: NEW})
    assert outcome == "hash-mismatch"
    assert run_case.store.load_flag(1).reason == Reason.HASH_MISMATCH
    assert run_case.executor.calls == []


def test_merge_with_an_old_main_parent_cannot_hide_a_rollback(run_case):
    outcome, _ = run_saved(run_case, {FIRST: OLD}, main_after={SECOND: NEW}, merge_old_main=True)
    assert outcome == "hash-mismatch"
    assert run_case.executor.calls == []


def test_own_lock_edit_that_conflicts_with_main_stops_without_merging(run_case):
    outcome, head = run_saved(run_case, {FIRST: "# issue edit\n"}, main_after={FIRST: NEW, SECOND: NEW})
    assert outcome == "hash-mismatch"
    assert run_case.executor.calls == []
    saved = [git_command(repo, "rev-parse", "--verify", "--quiet", BRANCH) or None
             for repo in (run_case.history.clone, run_case.history.origin)]
    assert head in saved, "The saved branch keeps its work and gains no half-made base merge"
    assert not any(sha and sha != head for sha in saved)


def test_own_lock_edit_beside_a_source_conflict_stops_before_the_resolver(run_case):
    outcome, _ = run_saved(run_case, {FIRST: "# issue edit\n", "src/x.py": "X = 'branch'\n"},
                           main_after={"src/x.py": "X = 'main'\n", SECOND: NEW})
    assert outcome == "hash-mismatch"
    assert run_case.executor.calls == []


def test_source_conflict_with_approved_locks_goes_to_the_resolver(run_case):
    outcome, _ = run_saved(run_case, {"src/x.py": "X = 'branch'\n"},
                           main_after={"src/x.py": "X = 'main'\n", FIRST: NEW, SECOND: NEW})
    assert outcome == "spec-question", run_case.store.load_flag(1)
    assert len(run_case.executor.calls) == 1


@pytest.mark.parametrize("republished", [SECOND, FIRST], ids=["another-lock", "same-lock"])
def test_wip_saved_during_a_base_merge_inherits_the_approved_locks_on_retry(run_case, republished):
    c = run_case
    configure(c)
    c.history.update({FIRST: OLD, SECOND: OLD, "src/x.py": "X = 0\n"})
    commit(c, c.history.tip, {"src/x.py": "X = 'branch'\n"})
    c.history.update({"src/x.py": "X = 'main'\n", FIRST: MID})  # source conflict plus a lock update
    approve(c)
    c.executor.steps = [{".foragent/QUESTION.md": "Stopped during the base merge."}]
    assert run_issue(1, c.ctx) == "spec-question"
    wip = git_command(c.history.clone, "rev-parse", BRANCH)
    # The quit merge is saved as an ordinary commit that carries main's lock bytes.
    assert len(git_command(c.history.clone, "rev-list", "--parents", "-n", "1", wip).split()) == 2
    assert git_command(c.history.clone, "show", f"{wip}:{FIRST}") == MID.strip()

    c.now[0] += timedelta(minutes=1)
    c.history.update({republished: NEW})  # the order owner republishes a lock, maybe the same one
    approve(c)
    seen = []
    c.executor.steps = [{".foragent/QUESTION.md": "Stopped again."}]
    run = c.executor.run

    def observe(**kwargs):
        seen.append({path: (kwargs["worktree"] / path).read_text() for path in (FIRST, SECOND)})
        return run(**kwargs)

    c.executor.run = observe
    if republished == FIRST:
        # The quit merge lost its merge parent, so the same lock conflicts again. An unresolved
        # lock conflict stops as hash-mismatch: no history says whose bytes the branch holds.
        assert run_issue(1, c.ctx) == "hash-mismatch"
        assert seen == []
        return
    assert run_issue(1, c.ctx) == "spec-question", c.store.load_flag(1)
    assert seen == [{FIRST: MID, SECOND: NEW}]


def test_own_edit_on_top_of_a_quit_base_merge_still_invalidates(run_case):
    c = run_case
    configure(c)
    c.history.update({FIRST: OLD, SECOND: OLD, "src/x.py": "X = 0\n"})
    commit(c, c.history.tip, {"src/x.py": "X = 'branch'\n"})
    c.history.update({"src/x.py": "X = 'main'\n", FIRST: MID})
    approve(c)
    c.executor.steps = [{".foragent/QUESTION.md": "Stopped during the base merge."}]
    assert run_issue(1, c.ctx) == "spec-question"
    assert c.store.get_run(1).merge_conflicts, "The saved WIP is a quit base merge"
    wip = git_command(c.history.clone, "rev-parse", BRANCH)
    git_command(c.history.origin, "fetch", c.history.clone, f"{wip}:refs/heads/{BRANCH}")
    git_command(c.history.clone, "update-ref", "-d", f"refs/heads/{BRANCH}")
    commit(c, wip, {SECOND: "# issue edit\n"})  # the branch's own lock edit after the quit merge

    c.now[0] += timedelta(minutes=1)
    c.history.update({FIRST: NEW})  # main republishes the lock the quit merge carried: a conflict
    approve(c)
    c.executor.steps = [{".foragent/QUESTION.md": "Must not start."}]
    calls = len(c.executor.calls)
    assert run_issue(1, c.ctx) == "hash-mismatch"
    assert len(c.executor.calls) == calls
