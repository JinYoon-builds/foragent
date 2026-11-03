"""Republished locks reach a WIP retry after an earlier base merge.

Exercise the public runner, approval tick and retry, using offline service fakes.
"""

from dataclasses import replace
from datetime import timedelta

import pytest

from conftest import git_command
from foragent import orch
from foragent.executors import ExecResult
from foragent.issue import body_hash
from foragent.runner import run_issue
from foragent.scheduler import TickContext, tick
from runner_support import run_case  # noqa: F401


FIRST = "tests/acceptance/test_flow_scorecard_split.py"
SECOND = "tests/acceptance/test_scorecard_flow.py"
BRANCH = "foragent/issue-1"
OLD = "def test_order_result():\n    assert 1 == 1\n"
CORRECTED = "def test_order_result():\n    assert 2 == 2\n"
REPUBLISHED = "def test_order_result():\n    assert 3 == 3\n"
WORK = "VALUE = 'saved source work'\n"


def republish_and_retry(case, files):
    """The order owner changes main, re-approves, then queues the public retry."""
    case.now[0] += timedelta(minutes=1)
    case.history.update(files, at=case.now[0])
    case.tracker.remove_labels(1, ["approved"])
    case.now[0] += timedelta(minutes=1)
    case.tracker.record_label(1, "approved", "human")
    orch.retry(case.ctx, 1, hint="The order owner corrected the locks. Keep the saved implementation.")
    context = TickContext(repo="owner/repo", repo_root=case.history.clone, tracker=case.tracker,
                          store=case.store, config=case.ctx.config, git=case.ctx.git,
                          now=lambda: case.now[0], launcher=lambda n: pytest.fail("Unexpected worker launch"),
                          start_runs=False, runner_process_exists=lambda n: False,
                          baseline_launcher=lambda *args: None)
    tick(context)


@pytest.mark.parametrize("saved_at", ["local", "origin"])
@pytest.mark.parametrize("next_edit", [SECOND, FIRST], ids=["another-lock", "same-lock"])
def test_republished_lock_reaches_wip_retry_after_an_earlier_main_merge(
    run_case, monkeypatch, saved_at, next_edit,
):
    c = run_case
    c.ctx.config = replace(c.ctx.config, review="fast", reviewer="none", checks="local",
                           skill_libraries=[], test_command="true", max_retries=0, approvers=["human"])
    c.history.update({FIRST: OLD, SECOND: OLD})
    c.ctx.git.fetch("origin", "main")
    body = ("<!-- foragent:v1 -->\n## Goal\nFinish the saved work.\n"
            "## Out of scope\nOther work.\n## Done when\n"
            "```sh\ntest -f feature.txt\n```\n"
            f"```sh\ntest -f {FIRST}\n```\n```sh\ntest -f {SECOND}\n```\n")
    c.tracker.update_issue(1, title="Finish the saved work", body=body)
    c.store.save_approval(1, body=body, body_sha=body_hash(body),
                          acceptance_blobs=c.ctx.git.blob_shas("origin/main", [FIRST, SECOND]),
                          approved_by="human", approved_at=c.now[0].isoformat())
    question = {"src/widget.py": WORK, ".foragent/QUESTION.md": "The order owner needs to correct a locked result."}
    c.executor.steps = [{**question, "src/widget.py": "VALUE = 'first partial implementation'\n"}, question]
    assert run_issue(1, c.ctx) == "spec-question"

    # A previous retry already merged one approved order correction from main.
    # That merge is inherited history, even when path history lists its commit.
    republish_and_retry(c, {FIRST: CORRECTED})
    assert run_issue(1, c.ctx) == "spec-question"
    old_base = c.history.tip
    wip = git_command(c.history.clone, "rev-parse", BRANCH)
    assert "WIP:" in git_command(c.history.clone, "log", "-1", "--format=%B", wip)
    assert git_command(c.history.clone, "diff", "--name-only", old_base, wip, "--", FIRST, SECOND) == ""
    assert git_command(c.history.clone, "show", f"{wip}:src/widget.py") == WORK.strip()
    if saved_at == "local":
        git_command(c.history.origin, "update-ref", "-d", f"refs/heads/{BRANCH}")
    else:
        git_command(c.history.clone, "update-ref", "-d", f"refs/heads/{BRANCH}")

    republish_and_retry(c, {next_edit: REPUBLISHED})
    expected = {FIRST: CORRECTED, SECOND: OLD, next_edit: REPUBLISHED}
    observed = []

    def finish(**kwargs):
        tree = kwargs["worktree"]
        observed.append({name: (tree / name).read_text() for name in (FIRST, SECOND)})
        assert observed[-1] == expected, "The retry must receive the current approved locked bytes"
        assert (tree / "src/widget.py").read_text() == WORK, "Keep the WIP's implementation work"
        (tree / "feature.txt").write_text("done")
        return ExecResult(0, False, "Finished the saved work", kwargs["log_path"])

    monkeypatch.setattr(c.executor, "run", finish)
    outcome = run_issue(1, c.ctx)
    assert outcome == "merged", f"Inherited main history must not strand the retry: {outcome}"
    assert observed == [expected], "Resume implementation once, using the approved files"
    assert c.tracker.get_issue(1).state == "closed"
    assert len(c.tracker.merged_prs) == 1
    assert "approved" in c.tracker.get_issue(1).labels
