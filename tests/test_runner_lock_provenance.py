"""A saved branch's lock difference is inherited only through facts outside the branch (#1679)."""

from dataclasses import replace
from datetime import timedelta

import pytest

from conftest import git_command
from runner_support import run_case  # noqa: F401
from foragent.git import ABSENT_BLOB
from foragent.issue import body_hash
from foragent.reasons import Reason
from foragent.run_publish_preserve import record_wip_locks, wip_locks
from foragent.runner import run_issue

BRANCH = "foragent/issue-1"
FIRST, SECOND = "tests/acceptance/test_first_lock.py", "tests/acceptance/test_second_lock.py"
OLD, MID, NEW = "# old\n", "# mid\n", "# new\n"


def blob(case, text):
    return git_command(case.history.origin, "hash-object", "--stdin", input=text)


def branch_commit(case, parent, files, message="Issue work"):
    """Write the issue's own commit with plumbing; a value of None deletes the path."""
    stream = [f"commit refs/heads/{BRANCH}\n", "committer Tests <t@example.com> 1792483200 +0000\n",
              f"data {len(message.encode())}\n{message}\nfrom {parent}\n"]
    for path, content in files.items():
        stream.append(f"D {path}\n" if content is None
                      else f"M 100644 inline {path}\ndata {len(content.encode())}\n{content}\n")
    git_command(case.history.origin, "fast-import", "--quiet", input="".join(stream) + "\ndone\n")
    return git_command(case.history.origin, "rev-parse", BRANCH)


def run_saved_branch(case, own, *, main_after, message="Issue work", record=None):
    case.ctx.config = replace(case.ctx.config, review="fast", max_retries=0, skill_libraries=[],
                              test_command="true", checks="local")
    case.history.update({FIRST: OLD, SECOND: OLD})
    case.history.update({FIRST: MID})
    fork = case.history.tip
    saved = branch_commit(case, fork, own, message)
    if record is not None:
        record_wip_locks(case.store, 1, saved, record(saved))
    case.history.update(main_after)          # main independently reaches the branch's bytes for FIRST
    case.history.update({SECOND: NEW})       # plus another locked change, so HEAD differs from approval
    case.ctx.git.fetch("origin", "main")
    body = ("<!-- foragent:v1 -->\n## Goal\nWork.\n## Out of scope\nNone.\n## Done when\n```sh\n"
            f"test -f {SECOND}\n```\n```sh\ntest -f src/x.py || test -f {FIRST}\n```\n")
    case.tracker.update_issue(1, title="Work", body=body)
    case.store.save_approval(1, body=body, body_sha=body_hash(body),
                             acceptance_blobs=case.ctx.git.blob_shas("origin/main", [FIRST, SECOND]),
                             approved_by="human", approved_at=case.now[0].isoformat())
    case.executor.steps = [{".foragent/QUESTION.md": "Stopped after implementation started."}]
    return run_issue(1, case.ctx)


@pytest.mark.parametrize("own,main_after", [
    ({FIRST: OLD}, {FIRST: OLD}),     # rollback to a historical blob that main also restores
    ({FIRST: None}, {FIRST: None}),   # deletion that main also makes (absent on both sides)
], ids=["rollback", "deletion"])
def test_own_lock_edit_matching_main_bytes_still_invalidates(run_case, own, main_after):
    assert run_saved_branch(run_case, own, main_after=main_after) == "hash-mismatch"
    assert run_case.store.load_flag(1).reason == Reason.HASH_MISMATCH
    assert run_case.executor.calls == []


@pytest.mark.parametrize("own,recorded", [({FIRST: OLD}, OLD), ({FIRST: None}, None)],
                         ids=["bytes", "absent"])
def test_a_branch_written_lock_claim_is_not_provenance(run_case, own, recorded):
    value = ABSENT_BLOB if recorded is None else blob(run_case, recorded)
    claim = f"WIP: Work (#1) [spec-question]\n\nForagent-Approved-Lock: {value} {FIRST}\n"
    assert run_saved_branch(run_case, own, main_after=own, message=claim) == "hash-mismatch"
    assert run_case.executor.calls == []


@pytest.mark.parametrize("own,recorded", [({FIRST: OLD}, OLD), ({FIRST: None}, None)],
                         ids=["bytes", "absent"])
def test_runner_record_of_the_saved_lock_is_inherited(run_case, own, recorded):
    value = ABSENT_BLOB if recorded is None else blob(run_case, recorded)
    outcome = run_saved_branch(run_case, own, main_after=own, record=lambda sha: {FIRST: value})
    assert outcome == "spec-question", run_case.store.load_flag(1)
    assert len(run_case.executor.calls) == 1


def test_runner_record_naming_other_bytes_or_commit_does_not_excuse_an_edit(run_case):
    other = blob(run_case, MID)
    outcome = run_saved_branch(run_case, {FIRST: OLD}, main_after={FIRST: OLD},
                               record=lambda sha: {FIRST: other})
    assert outcome == "hash-mismatch"
    assert run_case.executor.calls == []


def test_crafted_merge_with_an_old_main_parent_is_an_own_edit(run_case):
    c = run_case
    c.ctx.config = replace(c.ctx.config, review="fast", max_retries=0, skill_libraries=[],
                           test_command="true", checks="local")
    c.history.update({FIRST: OLD, SECOND: OLD})
    old_main = c.history.tip
    c.history.update({FIRST: MID})
    fork = c.history.tip
    # The branch "merges" an older main commit and keeps its old lock bytes: a rollback in disguise.
    stream = [f"commit refs/heads/{BRANCH}\n", "committer Tests <t@example.com> 1792483200 +0000\n",
              f"data 5\nMerge\nfrom {fork}\nmerge {old_main}\n",
              f"M 100644 inline {FIRST}\ndata {len(OLD)}\n{OLD}"]
    git_command(c.history.origin, "fast-import", "--quiet", input="".join(stream) + "\ndone\n")
    c.history.update({FIRST: OLD, SECOND: NEW})  # main independently matches it and changes another lock
    c.ctx.git.fetch("origin", "main")
    body = ("<!-- foragent:v1 -->\n## Goal\nWork.\n## Out of scope\nNone.\n## Done when\n```sh\n"
            f"test -f {SECOND}\n```\n```sh\ntest -f {FIRST}\n```\n")
    c.tracker.update_issue(1, title="Work", body=body)
    c.store.save_approval(1, body=body, body_sha=body_hash(body),
                          acceptance_blobs=c.ctx.git.blob_shas("origin/main", [FIRST, SECOND]),
                          approved_by="human", approved_at=c.now[0].isoformat())
    c.executor.steps = [{".foragent/QUESTION.md": "Stopped."}]
    assert run_issue(1, c.ctx) == "hash-mismatch"
    assert c.executor.calls == []


def test_merge_commit_that_edits_a_lock_itself_is_an_own_edit(run_case):
    c = run_case
    c.ctx.config = replace(c.ctx.config, review="fast", max_retries=0, skill_libraries=[],
                           test_command="true", checks="local")
    c.history.update({FIRST: OLD, SECOND: OLD})
    fork = c.history.tip
    own = branch_commit(c, fork, {"src/x.py": "X = 1\n"})
    c.history.update({"other.txt": "main work\n"})
    stream = [f"commit refs/heads/{BRANCH}\n", "committer Tests <t@example.com> 1792483200 +0000\n",
              f"data 5\nMerge\nfrom {own}\nmerge {c.history.tip}\n",
              f"M 100644 inline other.txt\ndata 10\nmain work\n",
              f"M 100644 inline {FIRST}\ndata {len(MID)}\n{MID}"]
    git_command(c.history.origin, "fast-import", "--quiet", input="".join(stream) + "\ndone\n")
    c.history.update({FIRST: MID, SECOND: NEW})  # main later approves the merge's own lock bytes
    c.ctx.git.fetch("origin", "main")
    body = ("<!-- foragent:v1 -->\n## Goal\nWork.\n## Out of scope\nNone.\n## Done when\n```sh\n"
            f"test -f {SECOND}\n```\n```sh\ntest -f {FIRST}\n```\n")
    c.tracker.update_issue(1, title="Work", body=body)
    c.store.save_approval(1, body=body, body_sha=body_hash(body),
                          acceptance_blobs=c.ctx.git.blob_shas("origin/main", [FIRST, SECOND]),
                          approved_by="human", approved_at=c.now[0].isoformat())
    c.executor.steps = [{".foragent/QUESTION.md": "Stopped."}]
    assert run_issue(1, c.ctx) == "hash-mismatch"
    assert c.executor.calls == []


def test_wip_saved_during_a_base_merge_names_its_locks_and_the_retry_inherits_them(run_case, monkeypatch):
    c = run_case
    c.ctx.config = replace(c.ctx.config, review="fast", max_retries=0, skill_libraries=[],
                           test_command="true", checks="local", approvers=["human"])
    c.history.update({FIRST: OLD, SECOND: OLD, "src/x.py": "X = 0\n"})
    branch_commit(c, c.history.tip, {"src/x.py": "X = 'branch'\n"})
    c.history.update({"src/x.py": "X = 'main'\n", FIRST: MID})  # source conflict plus a lock update

    def approve():
        body = ("<!-- foragent:v1 -->\n## Goal\nWork.\n## Out of scope\nNone.\n## Done when\n```sh\n"
                f"test -f {SECOND}\n```\n```sh\ntest -f {FIRST}\n```\n")
        c.ctx.git.fetch("origin", "main")
        c.tracker.update_issue(1, title="Work", body=body)
        c.store.save_approval(1, body=body, body_sha=body_hash(body),
                              acceptance_blobs=c.ctx.git.blob_shas("origin/main", [FIRST, SECOND]),
                              approved_by="human", approved_at=c.now[0].isoformat())

    approve()
    c.executor.steps = [{".foragent/QUESTION.md": "Stopped during the base merge."}]
    assert run_issue(1, c.ctx) == "spec-question"
    wip = git_command(c.history.clone, "rev-parse", BRANCH)
    assert len(git_command(c.history.clone, "rev-list", "--parents", "-n", "1", wip).split()) == 2
    assert wip_locks(c.store, 1)[wip] == {FIRST: blob(c, MID), SECOND: blob(c, OLD)}

    c.now[0] += timedelta(minutes=1)
    c.history.update({SECOND: NEW})  # the order owner republishes another lock
    approve()
    c.executor.steps = [{".foragent/QUESTION.md": "Stopped again."}]
    calls = len(c.executor.calls)
    assert run_issue(1, c.ctx) == "spec-question", c.store.load_flag(1)
    assert len(c.executor.calls) == calls + 1, "The retry must reach the implementer"
