from dataclasses import replace
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from foragent.config import Config
from foragent.executors import ExecResult
from foragent.git import GitRepo
from foragent.issue import body_hash, parse_body
from foragent.reasons import Reason, parse_reason
from foragent.runner import (
    REVIEW_PROMPT, CheckResult, RunContext, _batch_pr, build_prompt, build_review_prompt,
    parse_review, run_issue,
)
from foragent.state import StateStore
from foragent.tracker import FakeTracker, TrackerError

from conftest import ACCEPTANCE, BASE_TIME, git_command


COMMAND = 'test "$(cat feature.txt)" = done || { echo "feature not ready"; exit 1; }'
BODY = f"""<!-- foragent:v1 -->
## Goal
Implement the approved feature.
## Out of scope
Other features.
## Done when
```sh
{COMMAND}
```
```sh
test -f {ACCEPTANCE}
```
- [x] Looks right to a human
"""


class ScriptedExecutor:
    def __init__(self):
        self.steps = [{"feature.txt": "done"}]
        self.calls = []
        self.timed_out = False
        self.exit_code = 0

    def run(self, **kwargs):
        self.calls.append(kwargs)
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        for path, content in step.items():
            target = kwargs["worktree"] / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        return ExecResult(self.exit_code, self.timed_out, "untrusted model message", kwargs["log_path"])


def import_index(worktree, title):
    """Fake a commit via fast-import, without executing git commit."""
    parent = git_command(worktree, "rev-parse", "HEAD")
    branch = git_command(worktree, "symbolic-ref", "HEAD")
    stream = [
        f"commit {branch}\n", "committer foragent <foragent@localhost> 1792483200 +0000\n",
        f"data {len(title.encode())}\n{title}\n", f"from {parent}\n", "deleteall\n",
    ]
    index = git_command(worktree, "ls-files", "--stage", "-z")
    for entry in index.split("\0"):
        if entry:
            metadata, path = entry.split("\t", 1)
            mode, sha, stage = metadata.split()
            assert stage == "0"
            stream.append(f"M {mode} {sha} {json.dumps(path)}\n")
    stream.append("\ndone\n")
    git_command(worktree, "fast-import", "--quiet", input="".join(stream))


@pytest.fixture
def run_case(git_history, tmp_path, monkeypatch):
    now = [BASE_TIME + timedelta(hours=1)]
    tracker = FakeTracker(now=lambda: now[0], strict=True)
    tracker.add_issue(1, "Implement feature", BODY, labels=["approved", "running"])
    executor = ScriptedExecutor()
    store = StateStore(tmp_path / "state", now=lambda: now[0])
    store.save_approval(
        1, body=BODY, body_sha=body_hash(BODY),
        acceptance_blobs=GitRepo(git_history.clone).blob_shas("origin/main", [ACCEPTANCE]),
        approved_by="human", approved_at=now[0].isoformat(),
    )
    store.start_run(1, branch="foragent/issue-1", worktree=store.worktree_path(1))
    operations = []

    def git_run(args):
        root = git_history.clone
        if args[:1] == ["-C"]:
            root = Path(args[1])
            args = args[2:]
        operations.append((root, args))
        if args[:1] == ["-c"] and "commit" in args:
            import_index(root, args[-1])
            return ""
        if args[:1] == ["push"]:
            # The fake push transfers objects by fetching into the temporary bare origin.
            branch = args[-1]
            if ":" in branch:
                source, destination = branch.split(":", 1)
                sha = git_command(root, "rev-parse", source)
                git_command(git_history.origin, "fetch", str(root), f"{sha}:{destination}")
            else:
                git_command(git_history.origin, "fetch", str(root), f"+refs/heads/{branch}:refs/heads/{branch}")
                pr = tracker.find_pr(branch) or max(tracker.issues) + 1
                tracker.set_pr_head_sha(pr, git_command(root, "rev-parse", "HEAD"))
            return ""
        return subprocess.run(
            ["git", *args], cwd=root, check=True, capture_output=True, text=True, errors="replace",
        ).stdout

    context = RunContext(
        repo="owner/repo", repo_root=git_history.clone, tracker=tracker, store=store,
        config=Config(review="fast", max_retries=1, reviewer="none"), git=GitRepo(git_history.clone, git_run),
        now=lambda: now[0], executor_factory=lambda name: executor,
        sleep=lambda seconds: now.__setitem__(0, now[0] + timedelta(seconds=seconds)),
    )
    tracker.set_pr_files(2, ["feature.txt"])
    finished = []
    finish = store.finish_run

    def finish_once(*args, **kwargs):
        finished.append((args, kwargs))
        return finish(*args, **kwargs)

    monkeypatch.setattr(store, "finish_run", finish_once)
    case = SimpleNamespace(
        ctx=context, executor=executor, tracker=tracker, store=store, operations=operations,
        history=git_history, finished=finished, now=now,
    )
    yield case
    history = store.read_history()
    assert len(finished) == len(history) >= 1
    for run in history:
        assert not Path(run.worktree).exists()
        assert run.worktree not in git_command(git_history.clone, "worktree", "list")
        assert store.get_run(run.number).status == "finished"
        assert "running" not in tracker.get_issue(run.number).labels


@pytest.mark.parametrize("ci", ["pass", "none"])
def test_happy_path_merges_and_records_results(run_case, monkeypatch, ci):
    c = run_case
    c.tracker.set_pr_checks(2, ci)
    merge = c.tracker.merge_pr
    merged_heads = []

    def merge_verified(pr, *, method, expected_head_sha):
        assert expected_head_sha == git_command(c.history.origin, "rev-parse", "foragent/issue-1")
        merged_heads.append(expected_head_sha)
        merge(pr, method=method, expected_head_sha=expected_head_sha)

    monkeypatch.setattr(c.tracker, "merge_pr", merge_verified)
    assert run_issue(1, c.ctx) == "merged"
    assert len(merged_heads) == 1
    assert f"Pushed verified HEAD: {merged_heads[0]}" in c.store.log_path(1).read_text()
    assert c.tracker.merged_prs == [2]
    assert c.tracker.get_issue(1).state == "closed"
    assert c.tracker.get_issue(1).labels == {"approved"}
    pr = c.tracker.created_prs[0]
    assert pr.head == "foragent/issue-1" and pr.base == "main"
    assert pr.title == "Implement feature (#1)" and "Closes #1" in pr.body
    assert "| pass |" in pr.body and "## Needs a human check" in pr.body
    assert "- [ ] Looks right to a human" in pr.body and "- [x]" not in pr.body
    assert "Opened by Foragent gate (attempt 1)" in pr.body
    assert c.store.get_run(1).pid == os.getpid()
    assert c.store.get_run(1).outcome == "merged"
    assert "Check exit_code=0" in c.store.log_path(1).read_text()
    assert git_command(c.history.origin, "show", "foragent/issue-1:feature.txt") == "done"
    assert c.now[0] == BASE_TIME + timedelta(hours=1)


@pytest.mark.parametrize("already_closed", [False, True])
def test_merge_explicitly_closes_issue_and_removes_ready_and_running(run_case, monkeypatch, already_closed):
    c = run_case
    c.tracker.add_labels(1, ["ready"])
    merge = c.tracker.merge_pr
    close = c.tracker.close_issue
    closed = []

    def merge_pr(pr, **kwargs):
        merge(pr, **kwargs)
        if already_closed:
            close(1)

    def close_issue(number):
        assert c.tracker.merged_prs == [2]
        assert c.tracker.get_issue(number).state == ("closed" if already_closed else "open")
        closed.append(number)
        if already_closed:
            raise TrackerError("Issue is already closed")
        close(number)

    monkeypatch.setattr(c.tracker, "merge_pr", merge_pr)
    monkeypatch.setattr(c.tracker, "close_issue", close_issue)
    assert run_issue(1, c.ctx) == "merged"
    assert closed == [1]
    assert c.tracker.get_issue(1).state == "closed"
    assert c.tracker.get_issue(1).labels == {"approved"}
    assert c.store.get_run(1).outcome == "merged"
    assert c.store.load_flag(1) is None


def test_merge_does_not_ignore_close_failure_for_open_issue(run_case, monkeypatch):
    c = run_case

    def close_issue(number):
        raise TrackerError("HTTP 403 Forbidden")

    monkeypatch.setattr(c.tracker, "close_issue", close_issue)
    assert run_issue(1, c.ctx) == "error"
    assert c.tracker.merged_prs == [2]
    assert c.tracker.get_issue(1).state == "open"
    assert "needs-human" in c.tracker.get_issue(1).labels
    assert "HTTP 403 Forbidden" in c.tracker.list_comments(1)[-1].body


@pytest.mark.parametrize("when", ["opened", "polling", "checks", "merge"])
def test_changed_pr_head_is_held_as_risk_high(run_case, monkeypatch, when):
    c = run_case

    def change_head():
        c.tracker.set_pr_head_sha(2, "f" * 40)
        c.tracker.set_pr_files(2, ["feature.txt", ACCEPTANCE])

    if when == "opened":
        create = c.tracker.create_pr

        def create_changed(**kwargs):
            pr = create(**kwargs)
            change_head()
            return pr

        monkeypatch.setattr(c.tracker, "create_pr", create_changed)
    elif when == "polling":
        c.tracker.set_pr_checks(2, "pending")

        def sleep(seconds):
            c.now[0] += timedelta(seconds=seconds)
            change_head()
            c.tracker.set_pr_checks(2, "pass")

        c.ctx.sleep = sleep
    elif when == "checks":
        monkeypatch.setattr(c.tracker, "pr_checks", lambda pr: change_head() or "pass")
    else:
        merge = c.tracker.merge_pr

        def merge_changed(pr, **kwargs):
            change_head()
            merge(pr, **kwargs)

        monkeypatch.setattr(c.tracker, "merge_pr", merge_changed)

    assert run_issue(1, c.ctx) == "held"
    assert c.tracker.merged_prs == []
    assert c.tracker.get_issue(1).labels == {"approved", "needs-human"}
    assert c.store.load_flag(1).reason == Reason.RISK_HIGH
    comment = c.tracker.list_comments(1)[-1].body
    assert parse_reason(comment) == Reason.RISK_HIGH
    assert "PR head changed after verification" in comment


def set_done_when(case, commands):
    body = "## Done when\n" + "\n".join(f"```sh\n{command}\n```" for command in commands)
    approval = replace(case.store.load_approval(1), body=body, body_sha=body_hash(body))
    case.store.save_approval(**approval.__dict__)


def test_generated_source_is_rechecked_and_committed_instead_of_old_index(run_case):
    c = run_case
    c.executor.steps = [{
        "feature.py": "raise SystemExit(1)\n",
        "generate.sh": "printf 'print(42)\\n' > feature.py\n",
    }]
    set_done_when(c, ["sh generate.sh", "python3 feature.py"])
    assert run_issue(1, c.ctx) == "merged"
    assert git_command(c.history.origin, "show", "foragent/issue-1:feature.py") == "print(42)"
    assert c.store.log_path(1).read_text().count("Done when command:") == 4
    assert len(c.executor.calls) == 1


def test_second_verification_failure_reaches_executor_retry(run_case):
    c = run_case
    c.executor.steps = [{"feature.txt": "wrong"}, {"feature.txt": "done"}]
    # The first run changes a passing input into a failing one. Only the second
    # verification sees the failure; the executor must then repair it.
    set_done_when(c, [
        "if test ! -f .foragent/generated; then mkdir -p .foragent; "
        "touch .foragent/generated; echo broken > feature.txt; else "
        'test "$(cat feature.txt)" = done; fi',
    ])
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.executor.calls) == 2
    assert "A previous attempt failed" in c.executor.calls[1]["prompt"]
    assert git_command(c.history.origin, "show", "foragent/issue-1:feature.txt") == "done"


@pytest.mark.parametrize("command", [
    "printf x >> feature.txt",
    "if test -f feature.txt; then mv feature.txt other.txt; else mv other.txt feature.txt; fi",
])
def test_checks_that_keep_changing_blobs_or_paths_fail_attempt(run_case, command):
    c = run_case
    c.ctx.config = replace(c.ctx.config, max_retries=0)
    set_done_when(c, [command])
    assert run_issue(1, c.ctx) == "retries-exceeded"
    assert c.tracker.created_prs == c.tracker.merged_prs == []
    assert c.store.load_flag(1).reason == Reason.RETRIES_EXCEEDED
    assert "Done when commands modify tracked files" in c.tracker.list_comments(1)[-1].body
    assert c.store.log_path(1).read_text().count("Done when command:") == 2


@pytest.mark.parametrize("extension", ["yml", "yaml"])
@pytest.mark.parametrize("existing_pr", [False, True])
def test_workflows_wait_three_minutes_before_trusting_no_checks(run_case, monkeypatch, extension, existing_pr):
    c = run_case
    c.executor.steps[0][f".github/workflows/ci.{extension}"] = "name: CI\n"
    if existing_pr:
        c.tracker.create_pr(head="foragent/issue-1", base="main", title="Existing", body="Closes #1")
    # Time spent implementing the issue must not consume the registration grace.
    c.now[0] += timedelta(minutes=2)
    opened_at = c.now[0]
    polls = []

    def checks(pr):
        polls.append(c.now[0])
        assert c.tracker.merged_prs == []
        return "none"

    monkeypatch.setattr(c.tracker, "pr_checks", checks)
    assert run_issue(1, c.ctx) == "merged"
    assert polls == [opened_at + timedelta(seconds=30 * poll) for poll in range(7)]
    assert c.now[0] == opened_at + timedelta(minutes=3)
    assert c.tracker.merged_prs == [2] and len(c.tracker.created_prs) == 1


@pytest.mark.parametrize("statuses,outcome,elapsed", [
    (["pass"], "merged", 0),
    (["fail"], "ci-failed", 0),
    (["none", "pending", "pass"], "merged", 60),
    (["none", "pending", "fail"], "ci-failed", 60),
    (["none", *(["pending"] * 7), "pass"], "merged", 240),
    (["pending"] * 41, "timeout", 1200),
])
def test_workflow_checks_keep_pass_failure_and_pending_behavior(run_case, monkeypatch, statuses, outcome, elapsed):
    c = run_case
    c.executor.steps[0][".github/workflows/ci.yml"] = "name: CI\n"
    started_at = c.now[0]
    results = iter(statuses)
    monkeypatch.setattr(c.tracker, "pr_checks", lambda pr: next(results))
    assert run_issue(1, c.ctx) == outcome
    assert c.now[0] == started_at + timedelta(seconds=elapsed)
    assert c.tracker.merged_prs == ([2] if outcome == "merged" else [])


@pytest.mark.parametrize("path", [".github/workflows/readme.txt", ".github/workflows/nested/ci.yml"])
def test_non_workflow_files_do_not_delay_no_checks_merge(run_case, path):
    c = run_case
    c.executor.steps[0][path] = "not a workflow\n"
    started_at = c.now[0]
    assert run_issue(1, c.ctx) == "merged"
    assert c.now[0] == started_at


def test_workflow_removed_from_pr_head_does_not_delay_merge(run_case, monkeypatch):
    c = run_case
    c.history.update({".github/workflows/ci.yml": "name: CI\n"})
    execute = c.executor.run

    def remove_workflow(**kwargs):
        result = execute(**kwargs)
        (kwargs["worktree"] / ".github/workflows/ci.yml").unlink()
        return result

    monkeypatch.setattr(c.executor, "run", remove_workflow)
    started_at = c.now[0]
    assert run_issue(1, c.ctx) == "merged"
    assert c.now[0] == started_at


def test_retry_keeps_worktree_and_passes_failure_to_next_prompt(run_case):
    c = run_case
    c.executor.steps = [{"feature.txt": "wrong", "keep.txt": "keep me"}, {"feature.txt": "done"}]
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.executor.calls) == 2
    assert "A previous attempt failed. Output of the failing check:" in c.executor.calls[1]["prompt"]
    assert "feature not ready" in c.executor.calls[1]["prompt"]
    assert c.executor.calls[0]["worktree"] == c.executor.calls[1]["worktree"]
    assert c.store.get_run(1).attempts == 2
    assert "attempt 2" in c.tracker.created_prs[0].body
    assert git_command(c.history.origin, "show", "foragent/issue-1:keep.txt") == "keep me"


def test_exhausted_retries_preserve_failed_branch_and_do_not_open_pr(run_case):
    c = run_case
    c.executor.steps = [{"feature.txt": "wrong"}, {}]
    assert run_issue(1, c.ctx) == "retries-exceeded"
    assert c.tracker.created_prs == [] and c.tracker.merged_prs == []
    comment = c.tracker.list_comments(1)[-1].body
    assert parse_reason(comment) == Reason.RETRIES_EXCEEDED
    assert COMMAND in comment and "feature not ready" in comment
    assert git_command(c.history.origin, "show", "foragent/issue-1:feature.txt") == "wrong"
    assert len([args for _, args in c.operations if args[:1] == ["push"]]) == 1


def test_no_changes_are_a_failed_attempt_with_retry_output(run_case):
    c = run_case
    c.executor.steps = [{}, {}]
    assert run_issue(1, c.ctx) == "retries-exceeded"
    assert "The session made no changes." in c.executor.calls[1]["prompt"]
    assert "The session made no changes." in c.tracker.list_comments(1)[-1].body
    assert c.tracker.created_prs == []
    assert git_command(c.history.origin, "rev-parse", "foragent/issue-1") == c.history.tip


def test_question_stops_before_checks_and_is_truncated(run_case):
    c = run_case
    c.executor.steps = [{".foragent/QUESTION.md": "Choose A or B; recommend A.\n" + "q" * 7000}]
    assert run_issue(1, c.ctx) == "spec-question"
    comment = c.tracker.list_comments(1)[-1].body
    assert parse_reason(comment) == Reason.SPEC_QUESTION
    assert "Choose A or B; recommend A." in comment
    assert comment.endswith("q" * 100) and len(comment.split("\n\n", 1)[1]) == 6000
    assert not any(args[:1] == ["add"] for _, args in c.operations)
    assert c.tracker.created_prs == []


@pytest.mark.parametrize("path,labels,config,reason", [
    (ACCEPTANCE, [], {}, Reason.ACCEPTANCE_TOUCHED),
    ("auth/nested/login.py", [], {"always_human": ["auth/**"]}, Reason.ALWAYS_HUMAN_PATH),
    ("feature.txt", ["risk:high"], {}, Reason.RISK_HIGH),
])
def test_pr_is_opened_and_held_by_policy(run_case, path, labels, config, reason):
    c = run_case
    c.executor.steps = [{"feature.txt": "done", path: "done"}]
    c.tracker.add_labels(1, labels)
    c.ctx.config = replace(c.ctx.config, **config)
    c.tracker.set_pr_files(2, ["feature.txt", path])
    assert run_issue(1, c.ctx) == "held"
    assert len(c.tracker.created_prs) == 1 and c.tracker.merged_prs == []
    comment = c.tracker.list_comments(1)[-1].body
    assert parse_reason(comment) == reason and "https://github.com/owner/repo/pull/2" in comment


@pytest.mark.parametrize("config", [
    {"review": "batch"}, {"review": "careful"}, {"review_paths": {"**/*.txt": "careful"}},
])
def test_non_fast_leaves_pr_open_without_needs_human(run_case, config):
    c = run_case
    c.ctx.config = replace(c.ctx.config, **config)
    assert run_issue(1, c.ctx) == "pr-open"
    assert c.tracker.get_issue(1).labels == {"approved"}
    assert c.store.load_flag(1) is None and c.tracker.merged_prs == []
    assert "PR #2 is waiting for your review" in c.tracker.list_comments(1)[-1].body


@pytest.mark.parametrize("status,outcome,reason", [
    ("fail", "ci-failed", Reason.RETRIES_EXCEEDED), ("pending", "timeout", Reason.TIMEOUT),
])
def test_ci_failure_and_timeout(run_case, status, outcome, reason):
    c = run_case
    c.tracker.set_pr_checks(2, status)
    assert run_issue(1, c.ctx) == outcome
    assert c.store.load_flag(1).reason == reason
    assert "https://github.com/owner/repo/pull/2" in c.tracker.list_comments(1)[-1].body
    assert c.tracker.merged_prs == []
    if status == "pending":
        assert c.now[0] == BASE_TIME + timedelta(hours=1, minutes=20)


def test_ci_pending_polls_every_thirty_seconds_then_merges(run_case, monkeypatch):
    c = run_case
    statuses = iter(["pending", "pending", "pass"])
    monkeypatch.setattr(c.tracker, "pr_checks", lambda pr: next(statuses))
    assert run_issue(1, c.ctx) == "merged"
    assert c.now[0] == BASE_TIME + timedelta(hours=1, minutes=1)


@pytest.mark.parametrize("content", [None, "changed acceptance\n"])
def test_fetched_worktree_blob_mismatch_invalidates_approval(run_case, content):
    c = run_case
    c.history.update({ACCEPTANCE: content})
    assert run_issue(1, c.ctx) == "hash-mismatch"
    assert c.store.load_approval(1) is None and c.store.load_flag(1).reason == Reason.HASH_MISMATCH
    assert c.tracker.get_issue(1).labels == {"needs-human"}
    assert c.executor.calls == [] and c.tracker.created_prs == []


@pytest.mark.parametrize("expired", [False, True])
def test_session_timeout_and_exhausted_budget(run_case, expired):
    c = run_case
    if expired:
        c.now[0] += timedelta(minutes=c.ctx.config.max_runtime_minutes)
    else:
        c.executor.timed_out = True
    assert run_issue(1, c.ctx) == "timeout"
    assert c.store.load_flag(1).reason == Reason.TIMEOUT
    assert "Too big for one session; split it in order" in c.tracker.list_comments(1)[-1].body
    assert c.tracker.created_prs == []
    assert len(c.executor.calls) == (0 if expired else 1)


def test_remaining_budget_model_and_progress_are_passed_through(run_case):
    c = run_case
    c.ctx.config = replace(c.ctx.config, executor_model="chosen")
    c.now[0] += timedelta(minutes=2)
    assert run_issue(1, c.ctx) == "merged"
    assert c.executor.calls[0]["timeout_s"] == (90 - 2) * 60
    assert c.executor.calls[0]["model"] == "chosen"
    assert c.store.get_run(1).last_progress_at == c.now[0].isoformat()


def test_snapshot_supplies_prompt_checks_and_checklist_despite_live_body_and_comments(run_case):
    c = run_case
    c.tracker.edit_body(1, "## Goal\nLIVE UNAPPROVED\n## Done when\n```sh\nexit 99\n```\n")
    c.tracker.comment(1, "UNAPPROVED COMMENT")
    assert run_issue(1, c.ctx) == "merged"
    prompt = c.executor.calls[0]["prompt"]
    assert "Implement the approved feature." in prompt and "Other features." in prompt
    assert "LIVE UNAPPROVED" not in prompt and "UNAPPROVED COMMENT" not in prompt
    assert "exit 99" not in prompt and COMMAND in prompt
    assert "Looks right to a human" in c.tracker.created_prs[0].body


def test_missing_approval_finishes_without_execution(run_case):
    c = run_case
    c.store.clear_approval(1)
    assert run_issue(1, c.ctx) == "no-approval"
    assert c.executor.calls == [] and c.store.load_flag(1) is None


def test_stale_worktree_removed_and_rebuilt(run_case):
    c = run_case
    path = c.store.worktree_path(1)
    c.ctx.git.add_worktree(path, "foragent/issue-1", "origin/main")
    (path / "stale.txt").write_text("stale")
    assert run_issue(1, c.ctx) == "merged"
    assert "stale.txt" not in git_command(c.history.origin, "ls-tree", "-r", "--name-only", "foragent/issue-1")


def test_reuses_existing_pr(run_case):
    c = run_case
    c.tracker.create_pr(head="foragent/issue-1", base="main", title="Existing", body="Closes #1")
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.tracker.created_prs) == 1
    assert "## Review" in c.tracker.created_prs[0].body


def test_executor_nonzero_exit_still_relies_on_independent_checks(run_case):
    c = run_case
    c.executor.exit_code = 1
    assert run_issue(1, c.ctx) == "merged"


def test_unexpected_exception_is_logged_flagged_and_finished_once(run_case):
    c = run_case
    c.executor.steps = [RuntimeError("unexpected\nexecutor error")]
    assert run_issue(1, c.ctx) == "error"
    assert c.store.load_flag(1).reason == Reason.RETRIES_EXCEEDED
    assert "Runner failed: unexpected executor error" in c.tracker.list_comments(1)[-1].body
    assert "Traceback" in c.store.log_path(1).read_text()
    assert "RuntimeError: unexpected" in c.store.log_path(1).read_text()


def test_missing_machine_check_cannot_open_or_merge_pr(run_case):
    c = run_case
    approval = c.store.load_approval(1)
    c.store.save_approval(**{
        **approval.__dict__, "body": "## Goal\nMissing commands\n## Done when\n- [ ] Looks good\n",
    })
    assert run_issue(1, c.ctx) == "no-machine-check"
    assert c.executor.calls == [] and c.tracker.created_prs == []
    assert c.store.load_flag(1).reason == Reason.NO_MACHINE_CHECK


def test_check_timeout_uses_retry_limit_and_reports_command(run_case, monkeypatch):
    c = run_case
    c.executor.steps = [{"feature.txt": "done"}, {}]
    calls = []

    def check(command, worktree, log, timeout_s):
        calls.append((command, timeout_s))
        return CheckResult(command, -9, True, "slow check output")

    monkeypatch.setattr("foragent.runner._check", check)
    assert run_issue(1, c.ctx) == "retries-exceeded"
    assert calls == [(COMMAND, 900), (f"test -f {ACCEPTANCE}", 900)] * 2
    assert "slow check output" in c.executor.calls[1]["prompt"]
    assert "Check timed out." in c.tracker.list_comments(1)[-1].body
    assert c.tracker.created_prs == []


def test_retry_gets_remaining_budget_after_checks_spend_time(run_case, monkeypatch):
    c = run_case
    c.executor.steps = [{"feature.txt": "wrong"}, {"feature.txt": "done"}]
    from foragent.runner import _check

    def check(*args):
        c.now[0] += timedelta(minutes=1)
        return _check(*args)

    monkeypatch.setattr("foragent.runner._check", check)
    assert run_issue(1, c.ctx) == "merged"
    assert [call["timeout_s"] for call in c.executor.calls] == [90 * 60, 88 * 60]


def test_tracker_exception_after_push_still_cleans_up_and_finishes(run_case, monkeypatch):
    c = run_case

    def unavailable(**kwargs):
        raise RuntimeError("PR creation unavailable")

    monkeypatch.setattr(c.tracker, "create_pr", unavailable)
    assert run_issue(1, c.ctx) == "error"
    assert "PR creation unavailable" in c.store.log_path(1).read_text()
    assert c.store.load_flag(1).reason == Reason.RETRIES_EXCEEDED


def test_prompt_is_fixed_and_retry_tail_is_bounded():
    parsed = parse_body(BODY)
    prompt = build_prompt(7, parsed, "tests/acceptance/", "discard this" + "x" * 4000)
    assert prompt.startswith("You are implementing GitHub issue #7 in this repository.\n")
    assert "- Do not modify anything under tests/acceptance/. Those tests are fixed." in prompt
    assert "write .foragent/QUESTION.md" in prompt and "Do not commit, push or switch branches." in prompt
    assert "discard this" not in prompt and prompt.endswith("x" * 4000)


def enable_reviewer(case, messages, edit=None):
    calls = []
    messages = iter(messages)

    def review(**kwargs):
        calls.append(kwargs)
        if edit:
            edit(kwargs["worktree"])
        message = next(messages)
        if isinstance(message, ExecResult):
            return replace(message, log_path=kwargs["log_path"])
        return ExecResult(0, False, message, kwargs["log_path"])

    reviewer = SimpleNamespace(run=review)
    case.ctx.config = replace(case.ctx.config, reviewer=None, reviewer_model="review-model")
    case.ctx.executor_factory = lambda name: case.executor if name == "codex" else reviewer
    return calls


@pytest.mark.parametrize("message,note", [
    ("VERDICT: PASS", ""),
    ("preamble\nVERDICT: PASS\nfeature.txt:1: harmless observation\nVERDICT: FAIL", ""),
])
def test_review_pass_opens_pr_and_records_review(run_case, message, note):
    c = run_case
    calls = enable_reviewer(c, [message])
    c.store.log_path(1).parent.mkdir(parents=True, exist_ok=True)
    c.store.log_path(1).write_text("PRIVATE IMPLEMENTER LOG\n")
    assert run_issue(1, c.ctx) == "merged"
    assert len(calls) == 1 and len(c.executor.calls) == 1
    call = calls[0]
    assert call["model"] == "review-model"
    assert call["worktree"] == c.executor.calls[0]["worktree"]
    assert call["prompt"].startswith(REVIEW_PROMPT)
    assert "Implement the approved feature." in call["prompt"]
    assert "Other features." in call["prompt"] and COMMAND in call["prompt"]
    assert "diff --git a/feature.txt b/feature.txt" in call["prompt"]
    assert "PRIVATE IMPLEMENTER LOG" not in call["prompt"]
    assert "untrusted model message" not in call["prompt"]
    assert "Looks right to a human" not in call["prompt"]
    body = c.tracker.created_prs[0].body
    assert "## Review" in body and "Round 1: VERDICT: PASS" in body
    if note:
        assert note in body


def test_review_failure_retries_implementation_and_done_when(run_case):
    c = run_case
    c.executor.steps.append({})
    finding = "feature.txt:1: special-casing this input misses the actual goal"
    calls = enable_reviewer(c, [f"VERDICT: FAIL\n{finding}", "VERDICT: PASS"])
    assert run_issue(1, c.ctx) == "merged"
    assert c.store.get_run(1).attempts == 2
    assert finding in c.executor.calls[1]["prompt"]
    assert len(calls) == 2
    assert c.store.log_path(1).read_text().count("Done when command:") == 4
    body = c.tracker.created_prs[0].body
    assert "Round 1: VERDICT: FAIL" in body and finding in body
    assert "Round 2: VERDICT: PASS" in body
    assert finding not in calls[1]["prompt"]


@pytest.mark.parametrize("review_max,retries,rounds", [(1, 3, 2), (2, 2, 3)])
def test_exceeding_review_max_opens_pr_with_risk_high_hold(run_case, review_max, retries, rounds):
    c = run_case
    c.ctx.config = replace(c.ctx.config, review_max=review_max, max_retries=retries)
    c.executor.steps += [{}] * (rounds - 1)
    finding = "feature.txt:1: the stated goal is still broken"
    enable_reviewer(c, [f"VERDICT: FAIL\n{finding}"] * rounds)
    assert run_issue(1, c.ctx) == "held"
    assert len(c.tracker.created_prs) == 1 and not c.tracker.merged_prs
    assert c.store.get_run(1).attempts == rounds
    assert c.store.load_flag(1).reason == Reason.RISK_HIGH
    comment = c.tracker.list_comments(1)[-1].body
    assert f"Reviewer still found defects after {rounds} rounds" in comment
    assert finding in comment and "https://github.com/owner/repo/pull/2" in comment
    assert c.tracker.created_prs[0].body.count("VERDICT: FAIL") == rounds


def test_review_failure_uses_shared_retry_budget(run_case):
    c = run_case
    c.executor.steps = [{"feature.txt": "wrong"}, {"feature.txt": "done"}]
    calls = enable_reviewer(c, ["VERDICT: FAIL\nfeature.txt:1: goal still broken"])
    assert run_issue(1, c.ctx) == "retries-exceeded"
    assert len(calls) == 1 and len(c.executor.calls) == 2
    assert c.tracker.created_prs == []
    assert "goal still broken" in c.tracker.list_comments(1)[-1].body


def test_reviewer_edits_are_discarded_before_commit(run_case):
    c = run_case

    def edit(worktree):
        (worktree / "feature.txt").write_text("broken")
        (worktree / ACCEPTANCE).unlink()
        (worktree / "rogue.txt").write_text("must not reach PR")
        git_command(worktree, "add", "-A")
        (worktree / "feature.txt").write_text("unstaged reviewer edit")

    enable_reviewer(c, ["VERDICT: PASS"], edit)
    assert run_issue(1, c.ctx) == "merged"
    branch = "foragent/issue-1"
    assert git_command(c.history.origin, "show", f"{branch}:feature.txt") == "done"
    assert git_command(c.history.origin, "rev-parse", f"{branch}:{ACCEPTANCE}") == c.store.load_approval(1).acceptance_blobs[ACCEPTANCE]
    assert "rogue.txt" not in git_command(c.history.origin, "ls-tree", "-r", "--name-only", branch)
    assert "Reviewer changes discarded; staged index restored." in c.tracker.created_prs[0].body


def test_disabled_review_does_not_create_a_reviewer(run_case):
    c = run_case
    requested = []
    c.ctx.executor_factory = lambda name: requested.append(name) or c.executor
    assert run_issue(1, c.ctx) == "merged"
    assert requested == ["codex"]
    assert "Disabled (reviewer: none)" in c.tracker.created_prs[0].body


@pytest.mark.parametrize("invalid", [
    ExecResult(0, False, "", Path("unused")),
    ExecResult(0, False, "No defects found.", Path("unused")),
    ExecResult(0, False, "VERDICT: PASS", Path("unused"), full_message=""),
    ExecResult(1, False, "VERDICT: PASS", Path("unused")),
    ExecResult(None, False, "VERDICT: PASS", Path("unused")),
    ExecResult(0, True, "VERDICT: PASS", Path("unused")),
    ExecResult(0, False, "VERDICT: PASS-ish", Path("unused")),
    ExecResult(0, False, "VERDICT: PASS with caveats", Path("unused")),
])
@pytest.mark.parametrize("retry", ["pass", "fail", "invalid"])
def test_unusable_review_retries_once_and_only_explicit_success_allows_merge(run_case, invalid, retry):
    c = run_case
    second = {"pass": "VERDICT: PASS", "fail": "VERDICT: FAIL\nfeature.txt:1: broken", "invalid": invalid}[retry]
    calls = enable_reviewer(c, [invalid, second])
    assert run_issue(1, c.ctx) == ("merged" if retry == "pass" else "held")
    assert len(calls) == 2 and len(c.executor.calls) == 1
    assert c.store.get_run(1).attempts == 1
    assert len(c.tracker.created_prs) == 1
    body = c.tracker.created_prs[0].body
    assert "Round 1: VERDICT: UNUSABLE" in body and "Round 2: VERDICT:" in body
    if retry == "pass":
        assert c.tracker.merged_prs == [2] and c.store.load_flag(1) is None
    else:
        assert not c.tracker.merged_prs and c.tracker.get_issue(1).state == "open"
        assert c.store.load_flag(1).reason == Reason.RISK_HIGH
        assert "Reviewer did not return a usable verdict" in c.tracker.list_comments(1)[-1].body


def test_full_claude_verdict_survives_output_tail_truncation(run_case, monkeypatch):
    import sys
    from foragent.executors import ClaudeExecutor

    c = run_case
    c.executor.steps.append({})
    finding = "feature.txt:1: " + "x" * 930
    messages = iter(["VERDICT: FAIL\n" + "\n".join([finding] * 5), "VERDICT: PASS"])
    monkeypatch.setattr("foragent.executors.claude_command", lambda model: [
        sys.executable, "-c", f"print({next(messages)!r})",
    ])
    c.ctx.config = replace(c.ctx.config, reviewer="claude")
    c.ctx.executor_factory = lambda name: c.executor if name == "codex" else ClaudeExecutor()
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.executor.calls) == 2
    assert finding in c.executor.calls[1]["prompt"] or "x" * 900 in c.executor.calls[1]["prompt"]
    assert "Round 1: VERDICT: FAIL" in c.tracker.created_prs[0].body
    assert "Round 2: VERDICT: PASS" in c.tracker.created_prs[0].body


def test_reviewer_timeout_at_runtime_limit_still_opens_held_pr(run_case):
    c = run_case

    def consume_runtime(worktree):
        c.now[0] += timedelta(minutes=c.ctx.config.max_runtime_minutes)

    timeout = ExecResult(0, True, "VERDICT: PASS", Path("unused"))
    calls = enable_reviewer(c, [timeout, timeout], consume_runtime)
    assert run_issue(1, c.ctx) == "held"
    assert len(calls) == 2 and calls[1]["timeout_s"] == 0
    assert len(c.tracker.created_prs) == 1 and not c.tracker.merged_prs
    assert c.store.load_flag(1).reason == Reason.RISK_HIGH
    assert "Reviewer did not return a usable verdict" in c.tracker.list_comments(1)[-1].body


def test_reviewer_commit_is_detected_and_head_index_and_files_are_restored(run_case):
    c = run_case
    baseline = git_command(c.history.clone, "rev-parse", "origin/main")
    reviewer_heads = []

    def reviewer_commit(worktree):
        import_index(worktree, "Reviewer committed the implementation")
        reviewer_heads.append(git_command(worktree, "rev-parse", "HEAD"))

    enable_reviewer(c, ["VERDICT: PASS"], reviewer_commit)
    assert run_issue(1, c.ctx) == "merged"
    assert reviewer_heads[0] != baseline
    assert git_command(c.history.origin, "rev-parse", "foragent/issue-1^") == baseline
    assert git_command(c.history.origin, "show", "foragent/issue-1:feature.txt") == "done"
    assert "Reviewer changes discarded" in c.tracker.created_prs[0].body


@pytest.mark.parametrize("batch", [False, True])
def test_latin1_source_reaches_review_and_pr_without_corruption(run_case, monkeypatch, batch):
    c = run_case
    if batch:
        batch_setup(c)
        c.ctx.config = replace(c.ctx.config, review="fast", review_paths={"latin1.txt": "batch"})
    implement = c.executor.run

    def with_latin1(**kwargs):
        result = implement(**kwargs)
        (kwargs["worktree"] / "latin1.txt").write_bytes(b"caf\xe9\n")
        return result

    monkeypatch.setattr(c.executor, "run", with_latin1)
    calls = enable_reviewer(c, ["VERDICT: PASS"])
    assert run_issue(1, c.ctx) == "merged"
    assert "caf\ufffd" in calls[0]["prompt"]
    raw = subprocess.run(
        ["git", "show", "foragent/issue-1:latin1.txt"], cwd=c.history.origin, check=True, capture_output=True,
    ).stdout
    assert raw == b"caf\xe9\n"


def test_review_prompt_truncates_only_diff_and_parse_limits_findings():
    prompt = build_review_prompt(parse_body(BODY), "x" * 60000 + "TRUNCATED")
    assert prompt.endswith("x" * 60000) and "TRUNCATED" not in prompt
    review = parse_review("VERDICT: FAIL\n" + "\n".join(f"file:{i}: defect" for i in range(8)))
    assert review.verdict == "FAIL" and len(review.findings.splitlines()) == 5


def seed_batch(case, files):
    branch = "foragent/batch-99"
    stream = [
        f"commit refs/heads/{branch}\n",
        "committer Offline Tests <tests@example.com> 1792483200 +0000\n",
        f"data 7\nSibling\nfrom {case.history.tip}\n",
    ]
    for path, content in files.items():
        stream.append(f"M 100644 inline {path}\ndata {len(content.encode())}\n{content}\n")
    git_command(case.history.origin, "fast-import", "--quiet", input="".join(stream) + "\ndone\n")


def batch_setup(case):
    case.ctx.config = replace(case.ctx.config, review="batch")
    case.tracker.add_issue(99, "The whole feature")
    case.tracker.set_parent(1, 99)
    case.tracker.set_pr_files(100, ["feature.txt"])
    case.tracker.set_pr_checks(100, "pass")


@pytest.mark.parametrize("existing_batch", [False, True])
def test_batch_child_targets_shared_branch_closes_and_opens_parent_pr_once(run_case, existing_batch):
    c = run_case
    batch_setup(c)
    if existing_batch:
        seed_batch(c, {"sibling.txt": "previous child"})
    assert run_issue(1, c.ctx) == "merged"
    child, batch = c.tracker.created_prs
    assert child.base == "foragent/batch-99" and child.head == "foragent/issue-1"
    assert child.number in c.tracker.merged_prs and batch.number not in c.tracker.merged_prs
    assert c.tracker.get_issue(1).state == "closed"
    assert c.tracker.list_comments(1)[-1].body == "Merged into foragent/batch-99 (PR #100)"
    assert batch.head == "foragent/batch-99" and batch.base == "main"
    assert batch.title == "Batch: The whole feature"
    assert "Closes #99" in batch.body and "#1: Implement feature (PR #100)" in batch.body
    _batch_pr(99, c.ctx)
    assert len(c.tracker.created_prs) == 2
    if existing_batch:
        assert git_command(c.history.origin, "show", "foragent/issue-1:sibling.txt") == "previous child"
        assert not any(args[:2] == ["push", "origin"] for _, args in c.operations)
    else:
        assert any(args[:2] == ["push", "origin"] for _, args in c.operations)
    assert any(args == ["worktree", "add", "-B", "foragent/issue-1", str(c.store.worktree_path(1)), "origin/foragent/batch-99"]
               for _, args in c.operations)


def test_batch_waits_for_open_sibling_and_merge_unblocks_it(run_case):
    from foragent.scheduler import TickContext, tick
    from foragent.tracker import Dependency

    c = run_case
    batch_setup(c)
    c.tracker.add_issue(3, "Next child", BODY)
    c.tracker.record_label(3, "approved", "human")
    c.tracker.set_parent(3, 99)
    c.tracker.set_blocked_by(3, [Dependency(1, "open")])
    tick_ctx = TickContext(
        repo=c.ctx.repo, repo_root=c.ctx.repo_root, tracker=c.tracker, store=c.store,
        config=replace(c.ctx.config, approvers=["human"]), git=c.ctx.git, now=c.ctx.now,
        launcher=lambda number: None, start_runs=False,
    )
    tick(tick_ctx)
    assert "ready" not in c.tracker.get_issue(3).labels
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.tracker.created_prs) == 1
    tick(tick_ctx)
    assert "ready" in c.tracker.get_issue(3).labels


def test_batch_acceptance_binding_still_uses_approval_snapshot(run_case):
    c = run_case
    batch_setup(c)
    seed_batch(c, {ACCEPTANCE: "weakened by sibling"})
    assert run_issue(1, c.ctx) == "hash-mismatch"
    assert c.store.load_flag(1).reason == Reason.HASH_MISMATCH
    assert not c.executor.calls and not c.tracker.created_prs


def test_batch_still_checks_base_acceptance_when_batch_has_old_approved_copy(run_case):
    c = run_case
    batch_setup(c)
    seed_batch(c, {"sibling.txt": "previous child"})
    c.history.update({ACCEPTANCE: "changed on base after approval"})
    assert run_issue(1, c.ctx) == "hash-mismatch"
    assert c.store.load_flag(1).reason == Reason.HASH_MISMATCH
    assert not c.executor.calls


def test_reused_child_pr_is_retargeted_to_batch_and_gets_review_evidence(run_case):
    c = run_case
    batch_setup(c)
    c.tracker.create_pr(head="foragent/issue-1", base="main", title="Existing", body="Old body")
    enable_reviewer(c, ["VERDICT: PASS"])
    assert run_issue(1, c.ctx) == "merged"
    child = c.tracker.created_prs[0]
    assert child.base == "foragent/batch-99"
    assert "Round 1: VERDICT: PASS" in child.body


def test_path_override_replays_child_changes_on_batch_before_checks(run_case):
    c = run_case
    batch_setup(c)
    c.ctx.config = replace(c.ctx.config, review="fast", review_paths={"feature.txt": "batch"})
    seed_batch(c, {"sibling.txt": "previous child"})
    set_done_when(c, [COMMAND, "test -f sibling.txt"])
    assert run_issue(1, c.ctx) == "merged"
    assert c.tracker.created_prs[0].base == "foragent/batch-99"


@pytest.mark.parametrize("conflicting", [False, True])
def test_batch_patch_uses_three_way_merge_and_retries_real_conflicts(run_case, conflicting):
    c = run_case
    original = [f"line {i}\n" for i in range(1, 11)]
    c.history.update({"shared.txt": "".join(original)})
    sibling = original.copy()
    sibling[4] = "sibling change\n"
    batch_setup(c)
    c.ctx.config = replace(c.ctx.config, review="fast", review_paths={"shared.txt": "batch"})
    seed_batch(c, {"shared.txt": "".join(sibling)})
    child = original.copy()
    child[4 if conflicting else 2] = "child change\n"
    c.executor.steps = [{"feature.txt": "done", "shared.txt": "".join(child)}]
    if conflicting:
        resolved = sibling.copy()
        resolved[2] = "child change\n"
        c.executor.steps.append({"feature.txt": "done", "shared.txt": "".join(resolved)})
    assert run_issue(1, c.ctx) == "merged"
    shared = git_command(c.history.origin, "show", "foragent/issue-1:shared.txt").splitlines()
    assert shared[2] == "child change" and shared[4] == "sibling change"
    assert c.store.get_run(1).attempts == (2 if conflicting else 1)
    assert c.store.load_flag(1) is None
    assert any(args[:2] == ["apply", "--3way"] for _, args in c.operations)
    if conflicting:
        assert "Could not apply child changes" in c.executor.calls[1]["prompt"]


def test_tick_reconciles_manually_merged_careful_child_and_unblocks_sibling(run_case):
    from foragent.scheduler import TickContext, tick
    from foragent.tracker import Dependency

    c = run_case
    batch_setup(c)
    c.ctx.config = replace(c.ctx.config, review_paths={"feature.txt": "careful"})
    c.tracker.add_issue(3, "Next child", BODY)
    c.tracker.record_label(3, "approved", "human")
    c.tracker.set_parent(3, 99)
    c.tracker.set_blocked_by(3, [Dependency(1, "open")])
    assert run_issue(1, c.ctx) == "pr-open"
    child = c.tracker.created_prs[0]
    assert child.base == "foragent/batch-99"
    c.tracker.merge_pr(child.number, expected_head_sha=c.tracker.pr_head_sha(child.number))
    assert c.tracker.get_issue(1).state == "open"
    context = TickContext(
        repo=c.ctx.repo, repo_root=c.ctx.repo_root, tracker=c.tracker, store=c.store,
        config=replace(c.ctx.config, approvers=["human"]), git=c.ctx.git, now=c.ctx.now,
        launcher=lambda number: pytest.fail("reconciliation must not launch"), start_runs=False,
    )
    tick(context)
    assert c.tracker.get_issue(1).state == "closed"
    assert not c.tracker.get_issue(1).labels & {"ready", "running"}
    assert c.tracker.list_comments(1)[-1].body == f"Merged into foragent/batch-99 (PR #{child.number})"
    assert "ready" in c.tracker.get_issue(3).labels
    assert len(c.tracker.created_prs) == 1
    # Closing the remaining child completes the parent on a later tick.
    c.tracker.close_issue(3)
    tick(context)
    assert len(c.tracker.created_prs) == 2
    assert c.tracker.created_prs[-1].head == "foragent/batch-99"
    comments = c.tracker.list_comments(1)
    tick(context)
    assert c.tracker.list_comments(1) == comments and len(c.tracker.created_prs) == 2


def test_tick_retries_batch_finalization_after_last_child_is_already_closed(run_case, monkeypatch):
    from foragent.scheduler import TickContext, tick

    c = run_case
    batch_setup(c)
    create = c.tracker.create_pr
    unavailable = [True]

    def create_pr(**kwargs):
        if kwargs["head"] == "foragent/batch-99" and unavailable[0]:
            raise TrackerError("HTTP 503")
        return create(**kwargs)

    monkeypatch.setattr(c.tracker, "create_pr", create_pr)
    assert run_issue(1, c.ctx) == "merged"
    assert c.tracker.get_issue(1).state == "closed" and c.store.load_flag(1) is None
    assert len(c.tracker.created_prs) == 1
    context = TickContext(
        repo=c.ctx.repo, repo_root=c.ctx.repo_root, tracker=c.tracker,
        store=StateStore(c.store.root, now=c.ctx.now), config=c.ctx.config,
        git=c.ctx.git, now=c.ctx.now, launcher=lambda number: None, start_runs=False,
    )
    assert any("HTTP 503" in action for action in tick(context).actions)
    unavailable[0] = False
    tick(context)
    assert len(c.tracker.created_prs) == 2
    batch = c.tracker.created_prs[-1]
    assert batch.head == "foragent/batch-99" and "PR #100" in batch.body
    tick(context)
    assert len(c.tracker.created_prs) == 2
    c.tracker.merge_pr(batch.number, expected_head_sha=c.tracker.pr_head_sha(batch.number))
    tick(context)
    assert len(c.tracker.created_prs) == 2


def test_generated_file_selects_batch_before_final_verification(run_case):
    c = run_case
    batch_setup(c)
    c.ctx.config = replace(c.ctx.config, review="fast", review_paths={"generated.txt": "batch"})
    c.tracker.set_pr_files(100, ["feature.txt", "generated.txt"])
    seed_batch(c, {"sibling.txt": "previous child"})
    set_done_when(c, [COMMAND, "echo generated > generated.txt"])
    assert run_issue(1, c.ctx) == "merged"
    assert c.tracker.created_prs[0].base == "foragent/batch-99"
    assert c.store.log_path(1).read_text().count("Done when command:") == 4


def test_retry_removing_batch_override_returns_only_child_patch_to_base(run_case, monkeypatch):
    c = run_case
    batch_setup(c)
    c.ctx.config = replace(c.ctx.config, review="fast", review_paths={"batch.txt": "batch"})
    seed_batch(c, {"sibling.txt": "previous child"})
    c.executor.steps = [{"feature.txt": "done", "batch.txt": "unnecessary"}, {}]
    execute = c.executor.run

    def remove_batch_path(**kwargs):
        result = execute(**kwargs)
        if len(c.executor.calls) == 2:
            (kwargs["worktree"] / "batch.txt").unlink()
        return result

    monkeypatch.setattr(c.executor, "run", remove_batch_path)
    enable_reviewer(c, ["VERDICT: FAIL\nbatch.txt:1: remove this out-of-scope file", "VERDICT: PASS"])
    assert run_issue(1, c.ctx) == "merged"
    assert c.tracker.created_prs[0].base == "main"
    files = git_command(c.history.origin, "ls-tree", "-r", "--name-only", "foragent/issue-1")
    assert "feature.txt" in files and "sibling.txt" not in files and "batch.txt" not in files


def test_batch_ci_failure_keeps_child_open_and_does_not_open_parent_pr(run_case):
    c = run_case
    batch_setup(c)
    c.tracker.set_pr_checks(100, "fail")
    assert run_issue(1, c.ctx) == "ci-failed"
    assert c.tracker.get_issue(1).state == "open"
    assert not c.tracker.merged_prs and len(c.tracker.created_prs) == 1


def test_batch_siblings_build_on_actual_prior_merge(run_case, monkeypatch):
    c = run_case
    batch_setup(c)
    c.tracker.add_issue(2, "Second child", BODY, labels=["approved"])
    c.tracker.set_parent(2, 99)
    merge = c.tracker.merge_pr

    def merge_into_git_batch(pr, **kwargs):
        merge(pr, **kwargs)
        item = next(item for item in c.tracker.created_prs if item.number == pr)
        git_command(c.history.origin, "fetch", str(c.ctx.repo_root), f"+refs/heads/{item.head}:refs/heads/{item.base}")

    monkeypatch.setattr(c.tracker, "merge_pr", merge_into_git_batch)
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.tracker.created_prs) == 1  # Sibling still open.
    approval = c.store.load_approval(1)
    second_body = BODY.replace("Implement the approved feature.", "Add the second feature.")
    second_body += "```sh\ntest -f second.txt\n```\n"
    c.store.save_approval(
        2, body=second_body, body_sha=body_hash(second_body), acceptance_blobs=approval.acceptance_blobs,
        approved_by="human", approved_at=c.now[0].isoformat(),
    )
    c.now[0] += timedelta(seconds=1)
    c.tracker.add_labels(2, ["running"])
    c.store.start_run(2, branch="foragent/issue-2", worktree=c.store.worktree_path(2))
    c.executor.steps = [{"second.txt": "done"}]
    assert run_issue(2, c.ctx) == "merged"
    assert git_command(c.history.origin, "show", "foragent/batch-99:feature.txt") == "done"
    assert git_command(c.history.origin, "show", "foragent/batch-99:second.txt") == "done"
    assert c.tracker.get_issue(1).state == c.tracker.get_issue(2).state == "closed"
    assert c.tracker.merged_prs == [100, 101]
    batch = c.tracker.created_prs[-1]
    assert batch.head == "foragent/batch-99" and "PR #100" in batch.body and "PR #101" in batch.body
    assert batch.number not in c.tracker.merged_prs


def test_log_growth_poll_updates_progress_and_quiet_poll_does_not(run_case):
    c = run_case
    observed = []

    def poll_hook(execute, poll):
        result = execute()
        before = c.store.get_run(1).last_progress_at
        c.now[0] += timedelta(seconds=30)
        poll()
        assert c.store.get_run(1).last_progress_at == before
        c.store.log_path(1).parent.mkdir(parents=True, exist_ok=True)
        c.store.log_path(1).write_text("new executor output\n")
        c.now[0] += timedelta(seconds=30)
        poll()
        observed.append(c.store.get_run(1).last_progress_at)
        assert observed[-1] == c.now[0].isoformat()
        c.now[0] += timedelta(seconds=30)
        poll()
        assert c.store.get_run(1).last_progress_at == observed[-1]
        return result

    c.ctx.poll_hook = poll_hook
    assert run_issue(1, c.ctx) == "merged"
    assert len(observed) == 1
