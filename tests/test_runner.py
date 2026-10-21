from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
import fcntl
import json
import os
from pathlib import Path
import subprocess
import shlex
import sys
import threading
from types import SimpleNamespace

import pytest

from foragent.config import Config
from foragent.executors import ExecResult
from foragent.git import GitRepo
from foragent.issue import body_hash, parse_body
from foragent.reasons import Reason, parse_reason
from foragent.runner import (
    REVIEW_PROMPT, CheckResult, RunContext, _batch_pr, _executor_wait, _lane_skills, _merge_lock, _review, build_prompt, build_review_prompt,
    goal_condition, parse_code_review, parse_review, run_issue,
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
        f"data {len(title.encode())}\n{title}\n", f"from {parent}\n",
    ]
    merge_head = worktree / git_command(worktree, "rev-parse", "--git-path", "MERGE_HEAD")
    merging = merge_head.exists()
    if merging:
        stream.extend(f"merge {head}\n" for head in merge_head.read_text().splitlines())
    stream.append("deleteall\n")
    index = git_command(worktree, "ls-files", "--stage", "-z")
    for entry in index.split("\0"):
        if entry:
            metadata, path = entry.split("\t", 1)
            mode, sha, stage = metadata.split()
            assert stage == "0"
            stream.append(f"M {mode} {sha} {json.dumps(path)}\n")
    stream.append("\ndone\n")
    git_command(worktree, "fast-import", "--quiet", input="".join(stream))
    if merging:
        git_command(worktree, "merge", "--quit")


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
        monotonic=lambda: (now[0] - BASE_TIME).total_seconds(),
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


@pytest.mark.parametrize("mode,question,outcome,statuses", [
    ("fast", False, "merged", ["Running", "PR open", "Done"]),
    ("careful", False, "pr-open", ["Running", "PR open"]),
    ("fast", True, "spec-question", ["Running", "Blocked"]),
])
def test_runner_updates_project_on_execution_pr_merge_and_block(run_case, monkeypatch, mode, question, outcome, statuses):
    from foragent import board
    from test_board import item_values

    c = run_case
    project_id = board.ensure(c.tracker, c.ctx.repo)
    c.ctx.config = replace(c.ctx.config, board=project_id, review=mode)
    if question:
        c.executor.steps = [{".foragent/QUESTION.md": "Which behavior is intended?"}]
    calls = []
    set_status = board.set_status

    def recorded(issue, status):
        calls.append(status)
        set_status(issue, status)

    monkeypatch.setattr(board, "set_status", recorded)
    assert run_issue(1, c.ctx) == outcome
    assert calls == statuses
    assert item_values(c.tracker, project_id, 1)["Status"] == statuses[-1]


def test_board_transport_failure_does_not_fail_runner(run_case, caplog):
    from foragent import board

    c = run_case
    c.ctx.config = replace(c.ctx.config, board=board.ensure(c.tracker, c.ctx.repo))

    def unavailable(*args):
        raise OSError("offline project")

    c.tracker.graphql = unavailable
    assert run_issue(1, c.ctx) == "merged"
    assert c.tracker.get_issue(1).state == "closed"
    assert "Could not set board Status" in caplog.text


def test_merge_does_not_ignore_close_failure_for_open_issue(run_case, monkeypatch):
    c = run_case

    def close_issue(number):
        raise TrackerError("HTTP 403 Forbidden")

    monkeypatch.setattr(c.tracker, "close_issue", close_issue)
    assert run_issue(1, c.ctx) == "error"
    assert c.tracker.merged_prs == [2]
    assert c.tracker.get_issue(1).state == "open"
    assert "blocked" in c.tracker.get_issue(1).labels
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
    assert c.tracker.get_issue(1).labels == {"approved", "blocked"}
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
    assert c.tracker.get_issue(1).labels == {"blocked"}
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


@pytest.mark.parametrize("result", [
    "merged", "pr-open", "no-approval", "retries-exceeded", "limit-wait",
    RuntimeError("unexpected execution failure"), SystemExit(1),
])
def test_runner_kicks_after_cleanup_and_finish_for_every_outcome(run_case, monkeypatch, result, kicks):
    c = run_case

    def execute(number, ctx, worktree):
        ctx.tracker.remove_labels(number, ["running"])
        if isinstance(result, BaseException):
            raise result
        return result

    def kick(root):
        assert root == c.ctx.repo_root
        assert c.store.get_run(1).status == "finished"
        assert not c.store.worktree_path(1).exists()
        assert len(c.finished) == 1
        kicks.append(root)

    monkeypatch.setattr("foragent.runner._execute", execute)
    monkeypatch.setattr("foragent.runner.kick_tick", kick)
    if isinstance(result, SystemExit):
        with pytest.raises(SystemExit):
            run_issue(1, c.ctx)
    else:
        assert run_issue(1, c.ctx) == ("error" if isinstance(result, Exception) else result)
    assert kicks == [c.ctx.repo_root]
    assert c.store.get_run(1).outcome == (
        "killed" if isinstance(result, SystemExit) else "error" if isinstance(result, Exception) else result
    )


def test_runner_still_kicks_when_a_tick_already_finished_its_record(run_case, monkeypatch, kicks):
    c = run_case

    def execute(number, ctx, worktree):
        ctx.tracker.remove_labels(number, ["running"])
        ctx.store.finish_run(number, "killed")
        return "no-approval"

    monkeypatch.setattr("foragent.runner._execute", execute)
    assert run_issue(1, c.ctx) == "no-approval"
    assert c.store.get_run(1).outcome == "killed"
    assert len(c.finished) == 1
    assert kicks == [c.ctx.repo_root]


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


@pytest.mark.parametrize("language", ["ko", "en", "ja"])
def test_prompt_try_and_evidence_in_configured_language_on_every_attempt(run_case, language):
    c = run_case
    c.ctx.config = replace(c.ctx.config, language=language)
    c.executor.steps = [{"feature.txt": "not done"}, {"feature.txt": "done"}]
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.executor.calls) == 2
    for call in c.executor.calls:
        prompt = call["prompt"]
        assert f"Write it in {language}. Include an `## Evidence` section" in prompt
        assert "issue sentence; file:line and the code line" in prompt
        assert ".foragent/TRY.md" in prompt and f"Write the prose in {language}." in prompt
        assert all(heading in prompt for heading in ("## What changed", "## Try it", "## Expect", "## Web"))


@pytest.mark.parametrize("provided", [False, True])
@pytest.mark.parametrize("retarget", [False, True])
def test_success_stores_try_card_verbatim_in_pr_but_not_git(run_case, provided, retarget):
    c = run_case
    if retarget:
        batch_setup(c)
        c.ctx.config = replace(c.ctx.config, review="fast", review_paths={"feature.txt": "batch"})
    card = "## What changed\n결과를 보여 줍니다.\n## Try it\nkind: cli\n```sh\ncat feature.txt\n```\n## Expect\ndone 표시\n"
    if provided:
        c.executor.steps[0][".foragent/TRY.md"] = card
    enable_reviewer(c, ["VERDICT: PASS"])
    assert run_issue(1, c.ctx) == "merged"
    body = c.tracker.created_prs[0].body
    saved = c.store.root / "try/1.md"
    if provided:
        assert saved.read_text() == card
        assert body.endswith("## Try it\n\n" + card)
    else:
        assert not saved.exists()
        assert body.endswith("## Try it\n\n_not provided by the implementer_")
    files = git_command(c.history.origin, "ls-tree", "-r", "--name-only", "foragent/issue-1")
    assert ".foragent/" not in files


@pytest.mark.parametrize("outcome", ["merged", "held", "pr-open", "retries-exceeded", "retarget"])
def test_prestaged_try_and_other_artifacts_never_reach_branch(run_case, outcome):
    c = run_case
    if outcome == "retarget":
        batch_setup(c)
        c.ctx.config = replace(c.ctx.config, review="fast", review_paths={"feature.txt": "batch"})
    elif outcome == "held":
        c.tracker.add_labels(1, ["risk:high"])
    elif outcome == "pr-open":
        c.ctx.config = replace(c.ctx.config, review="careful")
    elif outcome == "retries-exceeded":
        c.executor.steps = [{"feature.txt": "wrong"}, {"feature.txt": "still wrong"}]
    card = "## Try it\nkind: cli\n```sh\ncat feature.txt\n```\n"
    implement = c.executor.run

    def prestage(**kwargs):
        result = implement(**kwargs)
        root = kwargs["worktree"]
        (root / ".foragent").mkdir(exist_ok=True)
        (root / ".foragent/TRY.md").write_text(card)
        (root / ".foragent/session.log").write_text("local execution artifact")
        git_command(root, "add", "-A")
        return result

    c.executor.run = prestage
    assert run_issue(1, c.ctx) == ("merged" if outcome == "retarget" else outcome)
    files = git_command(c.history.origin, "ls-tree", "-r", "--name-only", "foragent/issue-1")
    assert ".foragent/" not in files and "feature.txt" in files
    if outcome in {"merged", "retarget"}:
        assert (c.store.root / "try/1.md").read_text() == card


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
    case.ctx.config = replace(case.ctx.config, executor="claude", reviewer="codex", reviewer_model="review-model")
    case.ctx.executor_factory = lambda name: case.executor if name == "claude" else reviewer
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
    directory = c.store.root / "runs/issue-1"
    assert (directory / "review-1.txt").read_text() == f"VERDICT: FAIL\n{finding}"
    assert (directory / "review-2.txt").read_text() == "VERDICT: PASS"


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
    directory = c.store.root / "runs/issue-1"
    assert len(list(directory.glob("review-*.txt"))) == rounds
    assert all((directory / f"review-{number}.txt").read_text() == f"VERDICT: FAIL\n{finding}"
               for number in range(1, rounds + 1))


def test_review_failure_uses_shared_retry_budget(run_case):
    c = run_case
    c.executor.steps = [{"feature.txt": "wrong"}, {"feature.txt": "done"}]
    calls = enable_reviewer(c, ["VERDICT: FAIL\nfeature.txt:1: goal still broken"])
    assert run_issue(1, c.ctx) == "retries-exceeded"
    assert len(calls) == 1 and len(c.executor.calls) == 2
    assert c.tracker.created_prs == []
    assert "goal still broken" in c.tracker.list_comments(1)[-1].body


@pytest.mark.parametrize("findings,rebuttal,expected,attempts", [
    (["feature.txt:1 Payment support", "- feature.txt:1   PAYMENT support", "feature.txt:1 payment support"],
     True, "held", 3),
    (["feature.txt:1 payment support", "feature.txt:1 payment support", None], True, "merged", 3),
    (["feature.txt:1 payment support", "feature.txt:1 locale", "feature.txt:1 payment support", None],
     True, "merged", 4),
    (["feature.txt:1 payment support"] * 3 + [None], False, "merged", 4),
])
def test_review_dispute_requires_consecutive_findings_after_rebuttal(run_case, findings, rebuttal, expected, attempts):
    c = run_case
    c.ctx.config = replace(c.ctx.config, max_retries=3, review_max=4)
    c.executor.steps += ([{".foragent/REBUTTAL.md": "- payment support: outside the approved goal\n"}]
                         if rebuttal else [{}]) + [{}] * 2
    calls = enable_reviewer(c, [f"VERDICT: FAIL\n{point}" if point else "VERDICT: PASS" for point in findings])
    assert run_issue(1, c.ctx) == expected
    assert c.store.get_run(1).attempts == attempts
    assert len(calls) == len(c.executor.calls) == attempts
    if expected == "held":
        assert c.store.load_flag(1).reason == Reason.REVIEW_DISPUTE
        assert c.tracker.get_issue(1).labels & {"blocked", "needs-human"} == {"blocked"}
        assert "## Rebuttals" in c.tracker.created_prs[0].body
        assert not c.tracker.merged_prs
    else:
        assert c.store.load_flag(1) is None


def test_unstructured_reviews_with_shared_preamble_do_not_create_a_dispute(run_case):
    c = run_case
    c.ctx.config = replace(c.ctx.config, max_retries=3, review_max=4)
    c.executor.steps += [{".foragent/REBUTTAL.md": "- points: outside the approved goal\n"}, {}, {}]
    calls = enable_reviewer(c, [
        "Please inspect:\npayment support", "Please inspect:\nlocale support",
        "Please inspect:\nemail support", "VERDICT: PASS",
    ])
    assert run_issue(1, c.ctx) == "merged"
    assert len(calls) == 4
    assert c.store.load_flag(1) is None


def test_repeated_unstructured_finding_normalizes_wrapping_after_rebuttal(run_case):
    c = run_case
    c.ctx.config = replace(c.ctx.config, max_retries=3, review_max=4)
    c.executor.steps += [{".foragent/REBUTTAL.md": "- payment support: outside the approved goal\n"}, {}]
    calls = enable_reviewer(c, ["Needs payment support.", "Needs\npayment support.", "  NEEDS payment   support.\n"])
    assert run_issue(1, c.ctx) == "held"
    assert len(calls) == 3
    assert c.store.load_flag(1).reason == Reason.REVIEW_DISPUTE


def test_rebuttal_history_reaches_pr_and_codex_review_even_when_executor_stages_it(run_case, monkeypatch):
    c = run_case
    c.ctx.config = replace(c.ctx.config, max_retries=2, review_max=3)
    first = "- payment support: outside the approved goal\n"
    second = "- locale support: outside the approved goal\n"
    c.executor.steps += [{".foragent/REBUTTAL.md": first}, {".foragent/REBUTTAL.md": second}]
    implement = c.executor.run

    def stage_rebuttal(**kwargs):
        result = implement(**kwargs)
        if (kwargs["worktree"] / ".foragent/REBUTTAL.md").is_file():
            git_command(kwargs["worktree"], "add", "-f", ".foragent/REBUTTAL.md")
        return result

    monkeypatch.setattr(c.executor, "run", stage_rebuttal)
    calls = enable_reviewer(c, ["VERDICT: FAIL\nfeature.txt:1 payment support",
                                "VERDICT: FAIL\nfeature.txt:1 locale support", "VERDICT: PASS"])
    assert run_issue(1, c.ctx) == "merged"
    assert "Implementer rebuttals (judge them):\n" + first in calls[1]["prompt"]
    assert "Implementer rebuttals (judge them):\n" + second in calls[2]["prompt"]
    body = c.tracker.created_prs[0].body
    assert "## Rebuttals" in body and first in body and second in body
    assert ".foragent/REBUTTAL.md" not in git_command(c.history.origin, "ls-tree", "-r", "--name-only", "foragent/issue-1")


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
def test_unusable_review_retries_implementation_using_the_shared_budget(run_case, invalid, retry):
    c = run_case
    c.executor.steps.append({})
    second = {"pass": "VERDICT: PASS", "fail": "VERDICT: FAIL\nfeature.txt:1: broken", "invalid": invalid}[retry]
    calls = enable_reviewer(c, [invalid, second])
    assert run_issue(1, c.ctx) == ("merged" if retry == "pass" else "retries-exceeded")
    assert len(calls) == len(c.executor.calls) == 2
    assert c.store.get_run(1).attempts == 2
    raw = invalid.full_message if invalid.full_message is not None else invalid.last_message
    assert "A reviewer said (verbatim):\n" + raw in c.executor.calls[1]["prompt"]
    assert c.store.log_path(1).read_text().count("Done when command:") == 4
    if retry == "pass":
        assert c.tracker.merged_prs == [2] and c.store.load_flag(1) is None
        body = c.tracker.created_prs[0].body
        assert "Round 1: VERDICT: UNUSABLE" in body and "Round 2: VERDICT: PASS" in body
    else:
        assert not c.tracker.merged_prs and c.tracker.get_issue(1).state == "open"
        assert c.store.load_flag(1).reason == Reason.RETRIES_EXCEEDED


def test_full_claude_verdict_survives_output_tail_truncation(run_case, monkeypatch):
    import sys
    from foragent.executors import ClaudeExecutor

    c = run_case
    c.executor.steps.append({})
    finding = "- feature.txt:1 — " + "x" * 930
    messages = iter(["\n".join([finding] * 5), "(none)"])
    monkeypatch.setattr("foragent.executors.claude_review_command", lambda level, model, *, pr=None: [
        sys.executable, "-c", f"print({json.dumps({'result': next(messages)})!r})",
    ])
    c.ctx.config = replace(c.ctx.config, reviewer="claude")
    c.ctx.executor_factory = lambda name: c.executor if name == "codex" else ClaudeExecutor()
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.executor.calls) == 2
    assert finding in c.executor.calls[1]["prompt"] or "x" * 900 in c.executor.calls[1]["prompt"]
    assert "Round 1: VERDICT: FAIL" in c.tracker.created_prs[0].body
    assert "Round 2: VERDICT: PASS" in c.tracker.created_prs[0].body
    directory = c.store.root / "runs/issue-1"
    assert (directory / "review-1.txt").read_text() == "\n".join([finding] * 5)
    assert (directory / "review-2.txt").read_text() == "(none)"


def test_reviewer_timeout_at_runtime_limit_does_not_launch_more_sessions(run_case):
    c = run_case

    def consume_runtime(worktree):
        c.now[0] += timedelta(minutes=c.ctx.config.max_runtime_minutes)

    timeout = ExecResult(0, True, "VERDICT: PASS", Path("unused"))
    calls = enable_reviewer(c, [timeout], consume_runtime)
    assert run_issue(1, c.ctx) == "timeout"
    assert len(calls) == len(c.executor.calls) == 1
    assert not c.tracker.created_prs and not c.tracker.merged_prs
    assert c.store.load_flag(1).reason == Reason.TIMEOUT


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
def test_rebuttal_survives_batch_retarget_and_patch_recovery(run_case, conflicting):
    c = run_case
    c.history.update({"shared.txt": "base\n"})
    batch_setup(c)
    c.ctx.config = replace(c.ctx.config, review="fast", review_paths={"shared.txt": "batch"})
    seed_batch(c, {"shared.txt": "sibling\n"} if conflicting else {"sibling.txt": "sibling\n"})
    rebuttal = "- payment support: outside the approved goal\n"
    c.executor.steps[0].update({"shared.txt": "child\n", ".foragent/REBUTTAL.md": rebuttal})
    if conflicting:
        c.executor.steps.append({"feature.txt": "done", "shared.txt": "resolved\n"})
    calls = enable_reviewer(c, ["VERDICT: PASS"])
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.executor.calls) == (2 if conflicting else 1)
    assert "Implementer rebuttals (judge them):\n" + rebuttal in calls[0]["prompt"]
    assert rebuttal in c.tracker.created_prs[0].body
    assert ".foragent/REBUTTAL.md" not in git_command(c.history.origin, "ls-tree", "-r", "--name-only", "foragent/issue-1")


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


@pytest.mark.parametrize("before_push", [False, True])
@pytest.mark.parametrize("retries", [0, 1])
def test_fresh_base_generated_batch_path_replay_conflict_uses_remaining_attempts(run_case, monkeypatch, before_push, retries):
    c = run_case
    batch_setup(c)
    c.ctx.config = replace(c.ctx.config, review="fast", review_paths={"switch.txt": "batch"}, max_retries=retries)
    seed_batch(c, {"switch.txt": "sibling\n", "sibling.txt": "keep\n"})
    set_done_when(c, [COMMAND, "if test -f fresh.txt && ! test -f switch.txt; then echo child > switch.txt; fi"])
    c.executor.steps += [{"feature.txt": "done", "switch.txt": "resolved\n"}]
    moved = []
    execute = c.executor.run

    def move_base():
        c.history.update({"fresh.txt": "generate a batch path\n"})
        moved.append(c.history.tip)

    def implement(**kwargs):
        if c.executor.calls:
            root = kwargs["worktree"]
            assert GitRepo(root).conflict_paths() == []
            assert (root / "switch.txt").read_text() == "sibling\n"
            assert not (root / "feature.txt").exists()
        result = execute(**kwargs)
        if before_push and len(c.executor.calls) == 1:
            move_base()
        return result

    def ci(pr):
        if not moved:
            move_base()
        return "pass"

    monkeypatch.setattr(c.executor, "run", implement)
    monkeypatch.setattr(c.tracker, "pr_checks", ci)
    assert run_issue(1, c.ctx) == ("merged" if retries else "retries-exceeded")
    assert len(c.executor.calls) == c.store.get_run(1).attempts == retries + 1
    assert "Could not apply child changes to foragent/batch-99" in c.store.log_path(1).read_text()
    if retries:
        assert "Could not apply child changes to foragent/batch-99" in c.executor.calls[1]["prompt"]
        assert c.tracker.created_prs[0].base == "foragent/batch-99"
        assert git_command(c.history.origin, "show", "foragent/issue-1:switch.txt") == "resolved"
        assert git_command(c.history.origin, "show", "foragent/issue-1:sibling.txt") == "keep"
        assert c.store.load_flag(1) is None
    else:
        assert c.tracker.merged_prs == []
        assert c.store.load_flag(1).reason == Reason.RETRIES_EXCEEDED


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
        activity_before = c.store.get_run(1).last_activity_at
        c.now[0] += timedelta(seconds=30)
        poll()
        assert c.store.get_run(1).last_progress_at == before
        assert c.store.get_run(1).last_activity_at == activity_before
        c.store.log_path(1).parent.mkdir(parents=True, exist_ok=True)
        c.store.log_path(1).write_text("new executor output\n")
        c.now[0] += timedelta(seconds=30)
        poll()
        observed.append(c.store.get_run(1).last_progress_at)
        assert observed[-1] == c.now[0].isoformat()
        assert c.store.get_run(1).last_activity_at == observed[-1]
        c.now[0] += timedelta(seconds=30)
        poll()
        assert c.store.get_run(1).last_progress_at == observed[-1]
        assert c.store.get_run(1).last_activity_at == observed[-1]
        return result

    c.ctx.poll_hook = poll_hook
    assert run_issue(1, c.ctx) == "merged"
    assert len(observed) == 1


def test_short_executor_records_activity_without_a_periodic_poll(run_case, monkeypatch):
    c = run_case
    execute = c.executor.run

    def short_run(**kwargs):
        result = execute(**kwargs)
        c.now[0] += timedelta(seconds=7)
        kwargs["log_path"].parent.mkdir(parents=True, exist_ok=True)
        kwargs["log_path"].write_text("completed before the first poll\n")
        return result

    monkeypatch.setattr(c.executor, "run", short_run)
    c.ctx.poll_hook = lambda execute, poll: execute()
    assert run_issue(1, c.ctx) == "merged"
    assert c.store.get_run(1).last_activity_at == c.now[0].isoformat()


@pytest.mark.parametrize("acceptance_dir", ["tests/acceptance/", "checks with spaces", "./checks/"])
def test_claude_native_goal_settings_and_independent_retry(run_case, acceptance_dir):
    c = run_case
    c.ctx.config = replace(c.ctx.config, executor="claude", acceptance_dir=acceptance_dir)
    c.executor.steps = [{"feature.txt": "wrong"}, {"feature.txt": "done"}]
    assert run_issue(1, c.ctx) == "merged"
    settings_path = c.store.root / "runs/issue-1/claude-settings.json"
    settings = json.loads(settings_path.read_text())
    expected_command = shlex.join([
        sys.executable, "-m", "foragent.cli", "gate", "check-done",
        "--snapshot", str(c.store.root / "approvals/1.json"),
        "--worktree", str(c.store.worktree_path(1)), "--acceptance-dir", acceptance_dir,
    ])
    path = Path(acceptance_dir).as_posix().rstrip("/") + "/"
    assert settings == {
        "permissions": {"deny": [f"Edit({path}**)", f"Write({path}**)",
                                 "Bash(git commit:*)", "Bash(git push:*)",
                                 "Bash(git checkout:*)", "Bash(git switch:*)", "Bash(git merge:*)"]},
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": expected_command,
                                      "timeout": c.ctx.config.max_runtime_minutes * 60}]}]},
    }
    assert not settings_path.is_relative_to(c.store.worktree_path(1))
    assert (settings_path.parent / "stop-block-count").read_text() == "0"
    expected = (f'/goal Every command in the "Done when" list below exits 0 when run from the repository root, '
                f'nothing under {acceptance_dir} is modified, and no files outside this repository are changed. '
                'Or stop after 40 turns.\n\nYou are implementing GitHub issue #1')
    assert len(c.executor.calls) == 2
    for call in c.executor.calls:
        assert call["prompt"].startswith(expected)
        assert "Do not modify anything under" not in call["prompt"]
        assert COMMAND in call["prompt"] and "Other features." in call["prompt"]
        assert ".foragent/TRY.md" in call["prompt"]
        assert call["settings_path"] == settings_path
    assert "feature not ready" in c.executor.calls[1]["prompt"]
    assert c.store.log_path(1).read_text().count(f"Done when command:\n{COMMAND}") == 2


def test_native_goal_condition_length_cap_preserves_instructions():
    condition = goal_condition("a" * 5000)
    assert len(condition) == 3500
    assert condition.startswith('Every command in the "Done when" list below exits 0')
    assert condition.endswith("no files outside this repository are changed. Or stop after 40 turns.")


def test_claude_deny_paths_are_relative_even_for_absolute_acceptance_dir(run_case):
    c = run_case
    c.ctx.config = replace(c.ctx.config, executor="claude",
                           acceptance_dir=str(c.store.worktree_path(1) / "tests/acceptance"))
    assert run_issue(1, c.ctx) == "merged"
    settings = json.loads((c.store.root / "runs/issue-1/claude-settings.json").read_text())
    assert settings["permissions"]["deny"][:2] == ["Edit(tests/acceptance/**)", "Write(tests/acceptance/**)"]


@pytest.mark.parametrize("enabled", [False, True])
def test_codex_goal_opt_in_preserves_other_options_and_acceptance_rule(run_case, enabled):
    c = run_case
    c.ctx.config = replace(c.ctx.config, codex_goal=enabled)
    assert run_issue(1, c.ctx) == "merged"
    call = c.executor.calls[0]
    original = build_prompt(1, parse_body(BODY), c.ctx.config.acceptance_dir)
    assert call["prompt"] == (f"/goal {goal_condition(c.ctx.config.acceptance_dir)}\n\n" if enabled else "") + original
    assert set(call) == {"worktree", "prompt", "model", "timeout_s", "log_path", "effort"}
    assert call["effort"] == "high"
    assert not (c.store.root / "runs/issue-1").exists()


@pytest.mark.parametrize("event", [
    {"type": "system", "subtype": "init"},
    {"type": "assistant", "message": {"content": []}},
    {"type": "result", "result": ""},
])
def test_any_claude_stream_line_updates_progress_through_log_poll(run_case, event):
    c = run_case
    c.ctx.config = replace(c.ctx.config, executor="claude")
    observed = []

    def poll_hook(execute, poll):
        log = c.store.log_path(1)
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a") as stream:
            stream.write(json.dumps(event) + "\n")
        c.now[0] += timedelta(seconds=30)
        poll()
        observed.append(c.store.get_run(1).last_progress_at)
        assert observed[-1] == c.now[0].isoformat()
        return execute()

    c.ctx.poll_hook = poll_hook
    assert run_issue(1, c.ctx) == "merged"
    assert len(observed) == 1


@pytest.mark.parametrize("text,verdict", [
    ("\n(none)\n", "PASS"),
    ("(none)\nNo defects found.", "PASS"),
    ("- src/file.py:12 — broken feature", "FAIL"),
    ("- src/file.py:12 - broken feature", "FAIL"),
    ("preamble\n- src/file.py:12 -- broken feature", "FAIL"),
    ("- src/array[].py:12 — broken feature", "FAIL"),
    ("- src/file.py:12 — mishandles []", "FAIL"),
    ("- src/app/[123]/page.tsx:12 — broken feature", "FAIL"),
    ("(none)\n- file:1 — defect", "FAIL"),
    ("There are no changes to review.", "UNUSABLE"),
    ("No staged or unstaged changes.", "UNUSABLE"),
    ("No findings", "UNUSABLE"), ("", "UNUSABLE"),
    ("preamble\n(none)", "UNUSABLE"),
    ("- src/file.py:no-line — broken", "UNUSABLE"),
])
def test_code_review_result_parsing(text, verdict):
    result = parse_code_review(json.dumps({"result": text}))
    assert result.verdict == verdict
    if verdict == "FAIL":
        assert result.findings == next(line for line in text.splitlines() if line.startswith("- "))


def test_code_review_plain_finding_reports_failure():
    text = "src/foragent/adopt.py:180 — I couldn't confirm this one, because ..."
    result = parse_code_review(json.dumps({"result": text}))
    assert result.verdict == "FAIL"
    assert result.findings == text


def test_code_review_numbered_backtick_finding_reports_failure():
    text = '1. `src/foragent/design.py:50` — `path.read_text(encoding="utf-8")` has no error handling.'
    result = parse_code_review(json.dumps({"result": text}))
    assert result.verdict == "FAIL"
    assert result.findings == text


@pytest.mark.parametrize("message", ["garbage", "[]", "null", "{}", '{"result":null}',
                                          '{"result":"(none)","is_error":true}'])
def test_code_review_invalid_json_or_error_is_unusable(message):
    assert parse_code_review(message).verdict == "UNUSABLE"


REPORTED_JSON_FINDINGS = [
    {"file": "src/foragent/runner.py", "line": 121, "summary": "JSON 배열 지적이 판정 불가로 처리됨",
     "failure_scenario": '리뷰가 [{"file": "src/file.py"}] 형식이면 수정 대신 보류됨'},
    {"file": "src/foragent/cli.py", "line": 300, "summary": "결과가 누락됨",
     "failure_scenario": 'A quoted "value", a closing bracket ], and a path [name] are retained.'},
]


@pytest.mark.parametrize("wrapper", [
    "{}", " \n{}\n\t", "```json\n{}\n```", "```\n{}\n```",
    "```text\n{}\n```", "```python\n{}\n```",
    "[Review notes]\n```json\n{}\n```",
    "FAIL: foo([]) crashes.\n```json\n{}\n```\nEnd of review.",
    "FAIL: foo([]) crashes.\r\n```json\r\n{}\r\n```\r\nEnd of review.",
])
@pytest.mark.parametrize("items", [[], REPORTED_JSON_FINDINGS])
def test_code_review_accepts_reported_json_array_shape(wrapper, items):
    text = wrapper.format(json.dumps(items, ensure_ascii=False, indent=2))
    result = parse_code_review(json.dumps({"result": text}))
    assert result.verdict == ("FAIL" if items else "PASS")
    assert result.findings == "\n".join(f"{item['file']}:{item['line']} — {item['summary']}" for item in items)


@pytest.mark.parametrize("first,second", [([], REPORTED_JSON_FINDINGS), (REPORTED_JSON_FINDINGS, [])])
@pytest.mark.parametrize("wrapper", ["{}", "```json\n{}\n```"])
def test_code_review_multiple_json_arrays_are_unusable(first, second, wrapper):
    text = wrapper.format(json.dumps(first)) + "\n" + wrapper.format(json.dumps(second))
    result = parse_code_review(json.dumps({"result": text}))
    assert result.verdict == "UNUSABLE"


@pytest.mark.parametrize("items", [[], REPORTED_JSON_FINDINGS])
@pytest.mark.parametrize("wrapper", [
    "Review findings: {}\nEnd of review.", "FAIL: foo([]) crashes.\n{}",
    "FAIL: foo([]) crashes.\n```\n{}\n```", "Review:\n```python\n{}\n```",
    "Example `[]`, then {}", "[Review notes]\n{}",
])
def test_code_review_arrays_outside_findings_payload_are_unusable(wrapper, items):
    result = parse_code_review(json.dumps({"result": wrapper.format(json.dumps(items))}))
    assert result.verdict == "UNUSABLE"


@pytest.mark.parametrize("item", [
    {"file": "src/foo.py"}, {"line": 12}, {"summary": "Empty input crashes"},
    {"file": "src/foo.py", "line": 12}, {"summary": "mishandles []", "details": []},
])
def test_code_review_partial_findings_fail(item):
    result = parse_code_review(json.dumps({"result": json.dumps([item])}))
    assert result.verdict == "FAIL" and result.findings


@pytest.mark.parametrize("text", [
    "[", "[{", '[{"file":"x.py","line":1,"summary":"broken","details":[],}]',
    '[{"file":"x.py","line":1,"summary":"broken","details":[]}',
    '[{}, {"file":"x.py","line":1,"summary":"broken"}]',
    '["unexpected finding"]', '[null]', '[123]', '[[]]', '[{"details": []}]',
    '[{"summary":"broken"}, "unexpected"]', '[{"summary":"broken"}, null]',
    '(none)\n```json\n["unexpected"]\n```',
    '(none)\n```json\n[]\n```\n```json\n[]\n```',
])
def test_code_review_bad_arrays_are_unusable_without_passing_on_nested_empty_arrays(text):
    assert parse_code_review(json.dumps({"result": text})).verdict == "UNUSABLE"


def test_code_review_json_array_reports_every_item():
    items = [dict(REPORTED_JSON_FINDINGS[0], line=number) for number in range(1, 8)]
    result = parse_code_review(json.dumps({"result": json.dumps(items)}))
    assert result.verdict == "FAIL"
    assert result.findings.splitlines() == [f"src/foragent/runner.py:{number} — JSON 배열 지적이 판정 불가로 처리됨"
                                          for number in range(1, 8)]


def enable_native_reviewer(case, messages, edit=None):
    calls = []
    iterator = iter(messages)

    def review(**kwargs):
        calls.append(kwargs)
        assert "feature.txt" in git_command(kwargs["worktree"], "diff", "--cached", "--name-only")
        if edit:
            edit(kwargs["worktree"])
        message = next(iterator)
        if isinstance(message, ExecResult):
            return message
        return ExecResult(0, False, message, kwargs["log_path"])

    case.ctx.config = replace(case.ctx.config, reviewer="claude", review_level="xhigh")
    case.ctx.executor_factory = lambda name: case.executor if name == "codex" else SimpleNamespace(run=review)
    return calls


@pytest.mark.parametrize("last_result,outcome", [("(none)", "merged"), ("No verdict", "retries-exceeded")])
def test_pr_body_reviews_published_head_and_keeps_findings_in_comments(run_case, last_result, outcome):
    c = run_case
    c.ctx.config = replace(c.ctx.config, reviewer="claude", review_scope="pr-body")
    c.executor.steps.append({"fix.txt": "fixed"})
    finding = "feature.txt:1 — Fix the feature"
    messages = iter([finding, last_result])
    heads = []

    def review(**kwargs):
        pr = c.tracker.created_prs[0]
        assert kwargs["review_pr"] == pr.number
        assert not (kwargs["worktree"] / "CLAUDE.local.md").exists()
        assert "## Review scope" in pr.body and finding not in pr.body
        assert git_command(kwargs["worktree"], "diff", "--cached", "--name-only") == ""
        head = git_command(kwargs["worktree"], "rev-parse", "HEAD")
        assert head == git_command(c.history.origin, "rev-parse", "foragent/issue-1")
        assert head == c.tracker.pr_head_sha(pr.number)
        heads.append(head)
        return ExecResult(0, False, json.dumps({"result": next(messages)}), kwargs["log_path"])

    c.ctx.executor_factory = lambda name: c.executor if name == "codex" else SimpleNamespace(run=review)
    assert run_issue(1, c.ctx) == outcome
    assert len(heads) == 2 and heads[0] != heads[1]
    assert finding in c.executor.calls[1]["prompt"]
    comments = c.tracker.list_comments(c.tracker.created_prs[0].number)
    assert any(finding in comment.body for comment in comments)
    if outcome != "merged":
        assert any(last_result in comment.body for comment in comments)


@pytest.fixture
def review_context(git_history, tmp_path):
    now = BASE_TIME + timedelta(hours=1)
    git = GitRepo(git_history.clone)
    (git.root / "feature.txt").write_text("implementation")
    git.stage_changes()
    store = StateStore(tmp_path / "review-state", now=lambda: now)
    store.save_approval(
        1, body=BODY, body_sha=body_hash(BODY), acceptance_blobs={},
        approved_by="human", approved_at=now.isoformat(),
    )
    store.start_run(1, branch="foragent/issue-1", worktree=git.root)
    return RunContext(
        repo="owner/repo", repo_root=git.root, tracker=FakeTracker(), store=store,
        config=Config(), git=git, now=lambda: now,
        poll_hook=lambda execute, poll: execute(),
    )


def expected_review_rules(acceptance_dir):
    return (
        "Block only on: the Goal is not met, something listed in Out of scope was changed, or a check was weakened.\n"
        f"Tests under {acceptance_dir} that belong to other issues fail on purpose until those issues land; do not report them.\n"
        "Improvements beyond this issue's Goal are not defects."
    )


@pytest.mark.parametrize("original", [None, b"", b"# Personal instructions\r\nKeep exact bytes: \xff\n"],
                         ids=["absent", "empty", "existing-bytes"])
@pytest.mark.parametrize("outcome", ["pass", "fail", "unusable", "timeout", "nonzero", "exception", "force-stage"])
def test_claude_review_scope_restores_original_or_removes_temporary_file(review_context, original, outcome):
    ctx = review_context
    ctx.config = replace(ctx.config, acceptance_dir="locked checks/other/")
    path = ctx.git.root / "CLAUDE.local.md"
    if original is not None:
        path.write_bytes(original)
        path.chmod(0o640)
    tree = ctx.git.staged_tree_sha()
    head = ctx.git.head_sha()
    expected = (
        "# Review scope (written by Foragent for this review only)\n\n"
        + BODY + "\n\n" + expected_review_rules(ctx.config.acceptance_dir) + "\n\n"
    ).encode() + (original if original is not None else b"")
    calls = []

    def review(**kwargs):
        calls.append(kwargs)
        assert kwargs["prompt"] == "" and kwargs["review_level"] == "low"
        assert path.read_bytes() == expected
        assert git_command(ctx.git.root, "check-ignore", "CLAUDE.local.md") == "CLAUDE.local.md"
        assert "CLAUDE.local.md" not in ctx.git.changed_paths("HEAD")
        if outcome == "force-stage":
            git_command(ctx.git.root, "add", "-f", "--", "CLAUDE.local.md")
        path.write_bytes(b"reviewer changed the local instructions")
        if outcome == "exception":
            raise RuntimeError("reviewer crashed")
        raw = {"pass": "[]", "fail": '[{"summary":"Goal is not met"}]', "unusable": "unrecognized"}.get(outcome, "[]")
        return ExecResult(1 if outcome == "nonzero" else 0, outcome == "timeout",
                          json.dumps({"result": raw}), kwargs["log_path"])

    ctx.executor_factory = lambda name: SimpleNamespace(run=review)
    if outcome == "exception":
        with pytest.raises(RuntimeError, match="reviewer crashed"):
            _review(1, ctx, ctx.git, parse_body("Different parsed body"), "HEAD")
    else:
        result = _review(1, ctx, ctx.git, parse_body("Different parsed body"), "HEAD")
        assert result.verdict == {"pass": "PASS", "fail": "FAIL", "force-stage": "PASS"}.get(outcome, "UNUSABLE")
        assert any("changes discarded" in note for note in result.notes) == (outcome == "force-stage")
    assert len(calls) == 1
    if original is None:
        assert not path.exists()
    else:
        assert path.read_bytes() == original and path.stat().st_mode & 0o777 == 0o640
    assert ctx.git.staged_tree_sha() == tree and ctx.git.head_sha() == head
    assert git_command(ctx.git.root, "ls-files", "--", "CLAUDE.local.md") == ""


def test_claude_review_scope_does_not_accumulate_between_reviews(review_context):
    ctx = review_context
    path = ctx.git.root / "CLAUDE.local.md"
    original = b"Existing instructions\r\n"
    path.write_bytes(original)
    observed = []

    def review(**kwargs):
        observed.append(path.read_bytes())
        return ExecResult(0, False, '{"result":"[]"}', kwargs["log_path"])

    ctx.executor_factory = lambda name: SimpleNamespace(run=review)
    for round_number in (1, 2):
        assert _review(1, ctx, ctx.git, parse_body(BODY), "HEAD", round_number=round_number).verdict == "PASS"
        assert path.read_bytes() == original
    assert observed[0] == observed[1]
    assert observed[0].count(b"# Review scope (written by Foragent for this review only)") == 1
    exclude = ctx.git.root / ".git/info/exclude"
    assert exclude.read_bytes().splitlines().count(b"/CLAUDE.local.md") == 1


def test_claude_review_scope_preserves_a_local_instructions_symlink(review_context):
    ctx = review_context
    target = ctx.git.root.parent / "personal-instructions.md"
    target.write_bytes(b"Original target\r\n")
    path = ctx.git.root / "CLAUDE.local.md"
    path.symlink_to(target)

    def review(**kwargs):
        assert not path.is_symlink() and path.read_bytes().endswith(target.read_bytes())
        path.write_bytes(b"reviewer change")
        raise RuntimeError("reviewer crashed")

    ctx.executor_factory = lambda name: SimpleNamespace(run=review)
    with pytest.raises(RuntimeError, match="reviewer crashed"):
        _review(1, ctx, ctx.git, parse_body(BODY), "HEAD")
    assert path.is_symlink() and os.readlink(path) == str(target)
    assert target.read_bytes() == b"Original target\r\n"


def test_codex_review_gets_the_same_rules_without_a_temporary_scope_file(review_context):
    ctx = review_context
    ctx.config = replace(ctx.config, reviewer="codex", acceptance_dir="custom/locked-checks/")
    path = ctx.git.root / "CLAUDE.local.md"
    original = b"Personal instructions\r\n"
    path.write_bytes(original)

    def review(**kwargs):
        assert path.read_bytes() == original
        assert expected_review_rules(ctx.config.acceptance_dir) in kwargs["prompt"]
        assert "review_level" not in kwargs
        return ExecResult(0, False, "VERDICT: PASS", kwargs["log_path"])

    ctx.executor_factory = lambda name: SimpleNamespace(run=review)
    assert _review(1, ctx, ctx.git, parse_body(BODY), "HEAD").verdict == "PASS"
    assert path.read_bytes() == original


@pytest.mark.parametrize("fenced", [False, True])
def test_native_json_findings_trigger_repair_then_empty_array_passes_and_saves_raw_results(run_case, fenced):
    c = run_case
    c.executor.steps.append({})
    raw = json.dumps(REPORTED_JSON_FINDINGS, ensure_ascii=False, indent=2)
    if fenced:
        raw = f"```json\n{raw}\n```"
    calls = enable_native_reviewer(c, [json.dumps({"result": raw}), '{"result":"[]"}'])
    assert run_issue(1, c.ctx) == "merged"
    assert len(calls) == len(c.executor.calls) == 2
    for item in REPORTED_JSON_FINDINGS:
        assert f"{item['file']}:{item['line']} — {item['summary']}" in c.executor.calls[1]["prompt"]
    assert "Round 1: VERDICT: FAIL" in c.tracker.created_prs[0].body
    assert "Round 2: VERDICT: PASS" in c.tracker.created_prs[0].body
    directory = c.store.root / "runs/issue-1"
    assert (directory / "review-1.txt").read_text() == raw
    assert (directory / "review-2.txt").read_text() == "[]"
    assert "CLAUDE.local.md" not in git_command(c.history.origin, "ls-tree", "-r", "--name-only", "foragent/issue-1")
    assert not c.store.worktree_path(1).exists()


@pytest.mark.parametrize("fenced", [False, True])
def test_native_prose_empty_array_cannot_skip_defects_and_merge(run_case, fenced):
    c = run_case
    finding = {"file": "src/foo.py", "line": 12, "summary": "Empty input crashes"}
    payload = json.dumps([finding])
    raw = "FAIL: foo([]) crashes.\n" + (f"```json\n{payload}\n```" if fenced else payload)
    if fenced:
        c.executor.steps.append({})
        messages = [json.dumps({"result": raw}), '{"result":"[]"}']
    else:
        c.executor.steps.append({})
        messages = [json.dumps({"result": raw})] * 2
    calls = enable_native_reviewer(c, messages)
    assert run_issue(1, c.ctx) == ("merged" if fenced else "retries-exceeded")
    assert len(calls) == 2
    assert len(c.executor.calls) == 2
    if fenced:
        assert "src/foo.py:12 — Empty input crashes" in c.executor.calls[1]["prompt"]
        assert "Round 1: VERDICT: FAIL" in c.tracker.created_prs[0].body
    else:
        assert c.tracker.merged_prs == []
        assert "A reviewer said (verbatim):\n" + raw in c.executor.calls[1]["prompt"]
    assert (c.store.root / "runs/issue-1/review-1.txt").read_text() == raw


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("exit_code,timed_out", [(0, False), (1, False), (0, True)])
def test_unusable_review_saves_full_result_and_hands_off_full_raw_text(run_case, native, exit_code, timed_out):
    c = run_case
    c.executor.steps.append({})
    first = "First raw result\n" + "가" * 4000 + "\nFIRST END"
    second = "Latest raw result\n" + "나" * 4000 + "\nSECOND END"
    messages = [ExecResult(exit_code, timed_out, "only a tail", Path("unused"),
                           full_message=json.dumps({"result": raw}, ensure_ascii=False) if native else raw)
                for raw in (first, second)]
    (enable_native_reviewer if native else enable_reviewer)(c, messages)
    assert run_issue(1, c.ctx) == "retries-exceeded"
    assert "blocked" in c.tracker.get_issue(1).labels
    assert c.tracker.merged_prs == []
    directory = c.store.root / "runs/issue-1"
    assert (directory / "review-1.txt").read_text() == first
    assert (directory / "review-2.txt").read_text() == second
    assert "A reviewer said (verbatim):\n" + first in c.executor.calls[1]["prompt"]
    assert second in c.tracker.list_comments(1)[-1].body
    assert c.store.load_flag(1).reason == Reason.RETRIES_EXCEEDED


def test_unusable_review_followed_by_fail_keeps_both_raw_results(run_case):
    c = run_case
    c.executor.steps.append({})
    raw = "Unrecognized output\n" + "x" * 3500
    enable_native_reviewer(c, [json.dumps({"result": raw}), json.dumps({"result": json.dumps(REPORTED_JSON_FINDINGS)})])
    assert run_issue(1, c.ctx) == "retries-exceeded"
    assert "A reviewer said (verbatim):\n" + raw in c.executor.calls[1]["prompt"]
    assert (c.store.root / "runs/issue-1/review-1.txt").read_text() == raw
    assert c.store.load_flag(1).reason == Reason.RETRIES_EXCEEDED


def test_invalid_native_envelope_is_saved_and_handed_off_unchanged(run_case):
    c = run_case
    c.executor.steps.append({})
    raw = "Not a JSON response\n" + "x" * 3500
    enable_native_reviewer(c, [raw, raw])
    assert run_issue(1, c.ctx) == "retries-exceeded"
    assert (c.store.root / "runs/issue-1/review-2.txt").read_text() == raw
    assert "A reviewer said (verbatim):\n" + raw in c.executor.calls[1]["prompt"]


@pytest.mark.parametrize("invalid", [
    "garbage", json.dumps({"result": "No changes to review."}),
    json.dumps({"result": "(none)", "is_error": True}),
    ExecResult(1, False, '{"result":"(none)"}', Path("unused")),
])
@pytest.mark.parametrize("recover", [False, True])
def test_native_unusable_review_retries_implementation_then_passes_or_exhausts_budget(run_case, invalid, recover):
    c = run_case
    c.executor.steps.append({})
    calls = enable_native_reviewer(c, [invalid, '{"result":"(none)"}' if recover else invalid])
    assert run_issue(1, c.ctx) == ("merged" if recover else "retries-exceeded")
    assert len(calls) == len(c.executor.calls) == 2
    assert all(call["prompt"] == "" and call["review_level"] == "xhigh" for call in calls)
    if not recover:
        assert c.store.load_flag(1).reason == Reason.RETRIES_EXCEEDED
    else:
        assert "reviewer: /code-review xhigh" in c.tracker.created_prs[0].body


def test_native_review_findings_repair_and_reviewer_commits_are_restored(run_case):
    c = run_case
    c.executor.steps.append({})
    finding = "- feature.txt:1 — feature is broken"

    def edit(worktree):
        (worktree / "rogue.txt").write_text("reviewer edit")
        git_command(worktree, "add", "rogue.txt")
        import_index(worktree, "Reviewer commit")
        (worktree / "feature.txt").write_text("unstaged edit")

    calls = enable_native_reviewer(c, [json.dumps({"result": finding}), '{"result":"(none)"}'], edit)
    assert run_issue(1, c.ctx) == "merged"
    assert len(calls) == len(c.executor.calls) == 2
    assert finding in c.executor.calls[1]["prompt"]
    assert git_command(c.history.origin, "show", "foragent/issue-1:feature.txt") == "done"
    assert "rogue.txt" not in git_command(c.history.origin, "ls-tree", "-r", "--name-only", "foragent/issue-1")
    assert "Reviewer changes discarded" in c.tracker.created_prs[0].body


@pytest.mark.parametrize("kind", ["codex", "claude"])
def test_lane_executor_model_agent_notes_and_outside_paths(run_case, kind):
    c = run_case
    definition = "---\nname: fa-ui\nmemory: project\n---\nUse the UI skills.\n"
    memory = "".join(f"memory line {line}\n" for line in range(1, 202))
    c.history.update({".claude/agents/fa-ui.md": definition,
                      ".claude/agent-memory/fa-ui/MEMORY.md": memory})
    c.store.update_run(1, lane="ui")
    c.ctx.config = replace(c.ctx.config, executor_model="project-model", lanes={
        "ui": {"paths": ["src/**"], "agent": "fa-ui", "executor": kind, "model": "lane-model"},
    })
    factories = []
    c.ctx.executor_factory = lambda name: factories.append(name) or c.executor
    assert run_issue(1, c.ctx) == "merged"
    assert factories == [kind]
    call = c.executor.calls[0]
    assert call["model"] == "lane-model"
    if kind == "claude":
        assert call["agent"] == "fa-ui" and "settings_path" in call
        assert call["prompt"].startswith("/goal ")
    else:
        assert "agent" not in call
        assert call["prompt"].startswith("Lane notes:\nUse the UI skills.")
        assert "memory: project" not in call["prompt"]
        assert "memory line 200\n" in call["prompt"]
        assert "memory line 201" not in call["prompt"]
    assert "## Outside lane\n\n- `feature.txt`" in c.tracker.created_prs[0].body
    assert git_command(c.history.origin, "show", "foragent/issue-1:.claude/agent-memory/fa-ui/MEMORY.md") + "\n" == memory


@pytest.mark.parametrize("lane", ["main", "ui"])
def test_lane_null_overrides_and_main_use_project_defaults(run_case, lane):
    c = run_case
    c.store.update_run(1, lane=lane)
    c.ctx.config = replace(c.ctx.config, executor_model="project-model", lanes={
        "main": {"executor": "claude", "model": "ignored", "agent": "fa-main"},
        "ui": {"executor": None, "model": None, "agent": "fa-missing"},
    })
    assert run_issue(1, c.ctx) == "merged"
    call = c.executor.calls[0]
    assert call["model"] == "project-model" and "agent" not in call
    assert not call["prompt"].startswith("Lane notes:")
    if lane == "main":
        assert "## Outside lane" not in c.tracker.created_prs[0].body


@pytest.mark.parametrize("kind", ["codex", "claude"])
@pytest.mark.parametrize("source", ["config", "frontmatter", "empty-config"])
def test_lane_skills_come_from_worktree_and_only_extend_codex_prompts(run_case, source, kind):
    c = run_case
    definition = "---\nname: fa-ui\nskills:\n- agent-skill\n- missing\n---\nLane instructions.\n"
    c.history.update({
        ".claude/agents/fa-ui.md": definition,
        ".claude/skills/configured/SKILL.md": "---\nname: configured\ndescription: Skill metadata\n---\nConfigured strategy.\n",
        ".claude/skills/agent-skill/SKILL.md": "---\nname: agent-skill\ndescription: Skill metadata\n---\nAgent strategy.\n",
    })
    # The coordinator's local file must not replace the remote worktree's skill.
    local = c.ctx.repo_root / ".claude/skills/configured/SKILL.md"
    local.parent.mkdir(parents=True)
    local.write_text("Wrong local strategy.\n")
    lane = {"agent": "fa-ui", "executor": kind}
    if source != "frontmatter":
        lane["skills"] = ["configured", "missing"] if source == "config" else []
    c.ctx.config = replace(c.ctx.config, lanes={"ui": lane})
    c.store.update_run(1, lane="ui")
    assert run_issue(1, c.ctx) == "merged"
    call = c.executor.calls[0]
    pr = c.tracker.created_prs[0]
    if kind == "codex":
        name = "configured" if source == "config" else "agent-skill"
        assert f"- {name}: Skill metadata (.claude/skills/{name}/SKILL.md)\n\n" in call["prompt"]
        assert call["prompt"].startswith("Lane notes:\nLane instructions.\n\nLane skills (read the file when relevant and follow it):\n")
        assert "Configured strategy." not in call["prompt"] and "Agent strategy." not in call["prompt"]
        assert "description: Skill metadata" not in call["prompt"]
        assert "Wrong local strategy." not in call["prompt"]
        assert "missing skill: missing" in pr.body
        assert "agent" not in call
    else:
        assert call["agent"] == "fa-ui"
        assert call["prompt"].startswith("/goal ")
        assert "Lane skills (read the file" not in call["prompt"]
        assert "Configured strategy." not in call["prompt"] and "Agent strategy." not in call["prompt"]
        assert "missing skill:" not in pr.body
    assert local.read_text() == "Wrong local strategy.\n"


@pytest.mark.parametrize("agent", [None, "fa-missing"])
def test_codex_configured_skills_do_not_require_an_agent_file(run_case, agent):
    c = run_case
    c.history.update({".claude/skills/testing/SKILL.md": "---\ndescription: Focused checks\n---\nUse focused checks.\n"})
    c.ctx.config = replace(c.ctx.config, lanes={"ui": {"agent": agent, "skills": ["testing"]}})
    c.store.update_run(1, lane="ui")
    assert run_issue(1, c.ctx) == "merged"
    assert c.executor.calls[0]["prompt"].startswith("Lane skills (read the file when relevant and follow it):\n"
                                                "- testing: Focused checks (.claude/skills/testing/SKILL.md)\n\n")
    assert "Use focused checks." not in c.executor.calls[0]["prompt"]
    assert "missing skill:" not in c.tracker.created_prs[0].body


@pytest.fixture
def skill_case(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    ctx = SimpleNamespace(
        store=SimpleNamespace(get_run=lambda number: SimpleNamespace(lane="ui")),
        config=Config(lanes={"ui": {"skills": ["testing"]}}),
    )
    return ctx, worktree


@pytest.mark.parametrize("kind", ["codex", "claude"])
@pytest.mark.parametrize("source", ["local", "cache"])
def test_local_lane_skill_is_available_on_retries_and_never_published(run_case, tmp_path, monkeypatch, kind, source):
    c = run_case
    relative = ".claude/skills/testing"
    library = tmp_path / "library"
    skill = (c.ctx.repo_root / relative) if source == "local" else (library / "skills/testing")
    skill.mkdir(parents=True)
    content = "---\nname: testing\ndescription: Focused tests\n---\nRun useful checks.\n"
    (skill / "SKILL.md").write_text(content)
    (skill / "references").mkdir()
    (skill / "references/example.md").write_text("Reference\n")
    c.ctx.config = replace(c.ctx.config, executor=kind, lanes={"api": {"skills": ["testing"]}},
                           skill_libraries=[{"name": "fixture", "repo": "local", "path": str(library)}])
    c.store.update_run(1, lane="api")
    c.executor.steps = [{"feature.txt": "broken"}, {"feature.txt": "done"}]
    execute = c.executor.run

    def implement(**kwargs):
        worktree = kwargs["worktree"]
        assert (worktree / relative / "SKILL.md").read_text() == content
        assert (worktree / relative / "references/example.md").read_text() == "Reference\n"
        # Even explicitly staged copies must stay out of the published tree.
        git_command(worktree, "add", "-f", "--", relative)
        handmade = worktree / ".claude/skills/handmade/SKILL.md"
        handmade.parent.mkdir(parents=True, exist_ok=True)
        handmade.write_text("User-authored skill\n")
        return execute(**kwargs)

    monkeypatch.setattr(c.executor, "run", implement)
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.executor.calls) == 2
    published = git_command(c.history.origin, "ls-tree", "-r", "--name-only", "foragent/issue-1").splitlines()
    assert not any(name.startswith(relative + "/") for name in published)
    assert ".claude/skills/handmade/SKILL.md" in published


@pytest.mark.parametrize("length", [5999, 6000, 6001, 12000])
def test_lane_skill_list_includes_one_line_description_without_any_body(skill_case, length):
    ctx, worktree = skill_case
    path = worktree / ".claude/skills/testing/SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nname: testing\ndescription: |\n  Useful metadata\n  on two lines.\n---\n" + "가" * length)
    notes, missing = _lane_skills(ctx, 1, worktree, None)
    assert missing == []
    assert notes == ("Lane skills (read the file when relevant and follow it):\n"
                     "- testing: Useful metadata on two lines. (.claude/skills/testing/SKILL.md)\n\n")
    assert "가" not in notes and "[truncated]" not in notes


@pytest.mark.parametrize("missing_name", [None, "first", "sixth"])
def test_lane_skills_list_every_entry_in_order_with_no_count_limit(skill_case, missing_name):
    ctx, worktree = skill_case
    names = ["third", "first", "second", "fourth", "fifth", "sixth"]
    ctx.config.lanes["ui"]["skills"] = names
    for name in names:
        if name == missing_name:
            continue
        path = worktree / ".claude/skills" / name / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"---\ndescription: About {name}\n---\nInstructions for {name}.")
    notes, missing = _lane_skills(ctx, 1, worktree, None)
    selected = [name for name in names if name != missing_name]
    assert [line.split(":", 1)[0].removeprefix("- ") for line in notes.splitlines() if line.startswith("- ")] == selected
    assert missing == ([missing_name] if missing_name else [])
    assert ("- sixth: About sixth (.claude/skills/sixth/SKILL.md)" in notes) == (missing_name != "sixth")


@pytest.mark.parametrize("header", ["", "---\n---\n", "--- \nname: metadata\n--- \n"])
def test_lane_skills_without_a_description_keep_the_name_and_path(skill_case, header):
    ctx, worktree = skill_case
    path = worktree / ".claude/skills/testing/SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(header + "Testing instructions.\n")
    assert _lane_skills(ctx, 1, worktree, None) == (
        "Lane skills (read the file when relevant and follow it):\n- testing:  (.claude/skills/testing/SKILL.md)\n\n", [],
    )


@pytest.mark.parametrize("existing_pr", [False, True])
def test_missing_lane_skill_is_recorded_once_in_created_or_updated_pr_after_retry(run_case, existing_pr):
    c = run_case
    if existing_pr:
        c.tracker.create_pr(head="foragent/issue-1", base="main", title="Existing", body="old")
    c.ctx.config = replace(c.ctx.config, lanes={"ui": {"skills": ["missing"]}})
    c.store.update_run(1, lane="ui")
    c.executor.steps = [{"feature.txt": "not ready"}, {"feature.txt": "done"}]
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.executor.calls) == 2
    assert all("Lane skills (read the file" not in call["prompt"] for call in c.executor.calls)
    assert len(c.tracker.created_prs) == 1
    assert c.tracker.created_prs[0].body.count("missing skill: missing") == 1


def test_lane_skills_do_not_read_a_symlink_outside_the_worktree(skill_case, tmp_path):
    ctx, worktree = skill_case
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("Outside instructions.")
    directory = worktree / ".claude/skills"
    directory.mkdir(parents=True)
    (directory / "testing").symlink_to(outside, target_is_directory=True)
    assert _lane_skills(ctx, 1, worktree, None) == ("", ["testing"])


def test_unchanged_base_does_not_merge_or_repeat_checks(run_case):
    c = run_case
    assert run_issue(1, c.ctx) == "merged"
    assert not any("merge" in args for _, args in c.operations)
    assert c.store.log_path(1).read_text().count("Done when command:") == 2


@pytest.mark.parametrize("batch", [False, True])
def test_clean_fresh_base_rechecks_ci_and_merges_new_sha_without_review(run_case, monkeypatch, batch):
    c = run_case
    calls = enable_reviewer(c, ["VERDICT: PASS"])
    if batch:
        batch_setup(c)
    heads = []
    statuses = iter(["pass", "pending", "pass"])

    def checks(pr):
        heads.append(c.tracker.pr_head_sha(pr))
        if len(heads) == 1:
            if batch:
                seed_batch(c, {"parallel.txt": "fresh"})
            else:
                c.history.update({"parallel.txt": "fresh"})
        return next(statuses)

    monkeypatch.setattr(c.tracker, "pr_checks", checks)
    assert run_issue(1, c.ctx) == "merged"
    assert len(calls) == 1 and len(c.executor.calls) == 1
    assert len(heads) == 3 and heads[0] != heads[1] == heads[2]
    assert c.tracker.pr_head_sha(c.tracker.merged_prs[0]) == heads[-1]
    assert c.store.log_path(1).read_text().count("Done when command:") == 4
    assert git_command(c.history.origin, "show", "foragent/issue-1:parallel.txt") == "fresh"
    target = "foragent/batch-99" if batch else "main"
    target_sha = git_command(c.history.origin, "rev-parse", target)
    assert any(comment.body == f"base merged before merge: {target_sha}"
               for comment in c.tracker.list_comments(c.tracker.merged_prs[0]))


@pytest.fixture
def conflicting_lanes(run_case, monkeypatch):
    c = run_case
    c.ctx.config = replace(c.ctx.config, max_retries=2)
    c.history.update({"cli.py": "commands = ['help']\n"})
    c.executor.steps = [{"cli.py": "commands = ['help', 'issue']\n"}]
    c.tracker.set_pr_files(2, ["cli.py"])
    script = (
        "from cli import commands; from pathlib import Path; "
        "assert 'issue' in commands; "
        "assert not Path('base-feature.txt').exists() or 'base' in commands"
    )
    set_done_when(c, [f"{shlex.quote(sys.executable)} -B -c {shlex.quote(script)}"])
    c.ci_heads = []
    c.target_heads = []
    statuses = iter(["pass", "pending", "pass"])

    def move_target():
        c.history.update({"cli.py": "commands = ['help', 'base']\n", "base-feature.txt": "new base feature\n"})
        c.target_heads.append(c.history.tip)

    def ci(pr):
        c.ci_heads.append(c.tracker.pr_head_sha(pr))
        if not c.target_heads:
            move_target()
        return next(statuses)

    c.move_target = move_target
    monkeypatch.setattr(c.tracker, "pr_checks", ci)
    return c


@pytest.mark.parametrize("executor", ["codex", "claude"])
@pytest.mark.parametrize("before_push", [False, True])
def test_fresh_base_same_line_conflict_is_resolved_in_place_and_merge_commit_is_verified(conflicting_lanes, monkeypatch, executor, before_push):
    c = conflicting_lanes
    c.ctx.config = replace(c.ctx.config, executor=executor)
    c.executor.steps.append({"cli.py": "commands = ['help', 'base', 'issue']\n"})
    implement = c.executor.run
    merge = c.tracker.merge_pr
    implementation_heads = []

    def resolve(**kwargs):
        initial = not c.executor.calls
        assert c.store.get_run(1).phase == ("implementing" if initial else "resolving-conflict")
        if not initial:
            root = kwargs["worktree"]
            text = (root / "cli.py").read_text()
            assert all(marker in text for marker in ("<<<<<<<", "=======", ">>>>>>>"))
            assert "['help', 'issue']" in text and "['help', 'base']" in text
            assert git_command(root, "rev-parse", "MERGE_HEAD") == c.target_heads[0]
            assert GitRepo(root).conflict_paths() == ["cli.py"]
            assert c.store.get_run(1).attempts == 2
            implementation_heads.append(git_command(root, "rev-parse", "HEAD"))
            # The project merge lock remains held while the implementer resolves conflicts.
            with (c.store.root / "merge.lock").open("a") as lock:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = implement(**kwargs)
        if initial and before_push:
            c.move_target()
        return result

    def merge_verified(pr, *, method, expected_head_sha):
        root = c.store.worktree_path(1)
        assert c.store.get_run(1).phase == "merging"
        assert expected_head_sha == git_command(root, "rev-parse", "HEAD")
        assert not (root / git_command(root, "rev-parse", "--git-path", "MERGE_HEAD")).exists()
        parents = git_command(root, "show", "-s", "--format=%P", "HEAD").split()
        assert parents == [implementation_heads[0], c.target_heads[0]]
        merge(pr, method=method, expected_head_sha=expected_head_sha)

    monkeypatch.setattr(c.executor, "run", resolve)
    monkeypatch.setattr(c.tracker, "merge_pr", merge_verified)
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.executor.calls) == 2 and c.store.get_run(1).attempts == 2
    prompt = c.executor.calls[1]["prompt"]
    assert "Resolve the conflict markers in the following files:\n- cli.py" in prompt
    assert "Preserve both the features newly added on the base branch and the features for this issue." in prompt
    assert "Do not commit. The runner will stage the resolution and create the merge commit." in prompt
    if before_push:
        assert c.ci_heads == [c.tracker.pr_head_sha(2)]
    else:
        assert len(c.ci_heads) == 3 and c.ci_heads[0] != c.ci_heads[1] == c.ci_heads[2]
    assert "attempt 2" in c.tracker.created_prs[0].body
    assert c.store.log_path(1).read_text().count("Done when command:") == 2
    assert git_command(c.history.origin, "show", "foragent/issue-1:cli.py") == "commands = ['help', 'base', 'issue']"
    assert not any(args == ["merge", "--abort"] for _, args in c.operations)
    if executor == "claude":
        settings = json.loads(c.executor.calls[1]["settings_path"].read_text())
        assert "Bash(git merge:*)" in settings["permissions"]["deny"]


@pytest.mark.parametrize("width", [5, 7, 12])
@pytest.mark.parametrize("marker", ["<", "=", "|", ">"])
def test_conflict_marker_left_after_staging_consumes_attempt_before_merge_commit(conflicting_lanes, monkeypatch, width, marker):
    c = conflicting_lanes
    if width != 7:
        c.history.update({".gitattributes": f"cli.py conflict-marker-size={width}\n"})
    git_command(c.history.clone, "config", "merge.conflictStyle", "diff3")
    c.executor.steps += [
        {"cli.py": f"commands = ['help', 'base', 'issue']\n{marker * width}\n"},
        {"cli.py": "commands = ['help', 'base', 'issue']\n"},
    ]
    implement = c.executor.run

    def prestage(**kwargs):
        if len(c.executor.calls) == 1:
            content = (kwargs["worktree"] / "cli.py").read_text()
            assert f"{'<' * width} HEAD\n" in content and f"{'|' * width} " in content
        result = implement(**kwargs)
        if len(c.executor.calls) == 2:
            git_command(kwargs["worktree"], "add", "cli.py")
        if len(c.executor.calls) == 3:
            assert git_command(kwargs["worktree"], "rev-parse", "MERGE_HEAD") == c.target_heads[0]
        return result

    monkeypatch.setattr(c.executor, "run", prestage)
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.executor.calls) == 3 and c.store.get_run(1).attempts == 3
    assert "Conflict markers remain in: cli.py" in c.executor.calls[2]["prompt"]
    assert "Resolve the conflict markers" in c.executor.calls[2]["prompt"]
    assert c.store.log_path(1).read_text().count("Done when command:") == 2
    assert len(git_command(c.history.origin, "show", "-s", "--format=%P", "foragent/issue-1").split()) == 2


@pytest.mark.parametrize("width", [5, 12])
def test_noop_conflict_resolver_is_retried_even_when_done_when_checks_other_files(conflicting_lanes, width):
    c = conflicting_lanes
    c.history.update({".gitattributes": f"cli.py conflict-marker-size={width}\n"})
    set_done_when(c, [f"test -f {ACCEPTANCE}"])
    c.executor.steps += [{}, {"cli.py": "commands = ['help', 'base', 'issue']\n"}]
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.executor.calls) == 3 and c.store.get_run(1).attempts == 3
    assert "Conflict markers remain in: cli.py" in c.executor.calls[2]["prompt"]
    assert git_command(c.history.origin, "show", "foragent/issue-1:cli.py") == "commands = ['help', 'base', 'issue']"


def test_done_when_reintroduced_ancestor_marker_in_conflict_file_consumes_attempt(conflicting_lanes):
    c = conflicting_lanes
    set_done_when(c, [
        f"test -f {ACCEPTANCE}",
        "if test -f base-feature.txt && ! test -f fixed.txt; then printf '||||||| ancestor\\n' > cli.py; fi",
    ])
    resolved = "commands = ['help', 'base', 'issue']\n"
    c.executor.steps += [{"cli.py": resolved}, {"cli.py": resolved, "fixed.txt": "done"}]
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.executor.calls) == 3 and c.store.get_run(1).attempts == 3
    assert "Conflict markers remain in: cli.py" in c.executor.calls[2]["prompt"]
    assert git_command(c.history.origin, "show", "foragent/issue-1:cli.py") == resolved.strip()


def test_conflict_resolution_done_when_failure_uses_remaining_attempt_on_merged_base(conflicting_lanes, monkeypatch):
    c = conflicting_lanes
    c.executor.steps += [
        {"cli.py": "commands = ['help', 'issue']\n"},
        {"cli.py": "commands = ['help', 'base', 'issue']\n"},
    ]
    implement = c.executor.run

    def fix_checks(**kwargs):
        if len(c.executor.calls) == 2:
            root = kwargs["worktree"]
            assert (root / "base-feature.txt").exists()
            assert GitRepo(root).is_ancestor(c.target_heads[0], git_command(root, "rev-parse", "HEAD"))
            assert not (root / git_command(root, "rev-parse", "--git-path", "MERGE_HEAD")).exists()
        return implement(**kwargs)

    monkeypatch.setattr(c.executor, "run", fix_checks)
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.executor.calls) == 3 and c.store.get_run(1).attempts == 3
    assert "Command:" in c.executor.calls[2]["prompt"] and "Resolve the conflict markers" in c.executor.calls[2]["prompt"]
    assert "attempt 3" in c.tracker.created_prs[0].body
    assert c.store.log_path(1).read_text().count("Done when command:") == 3
    assert c.ci_heads[0] != c.ci_heads[1] == c.ci_heads[2]


@pytest.mark.parametrize("source", ["implementation", "resolution", "checks"])
def test_conflict_resolution_ignores_section_underlines_in_other_changed_files(conflicting_lanes, source):
    c = conflicting_lanes
    readme = "Heading\n=======\n"
    resolution = {"cli.py": "commands = ['help', 'base', 'issue']\n"}
    if source == "implementation":
        c.executor.steps[0]["README.rst"] = readme
    elif source == "resolution":
        resolution["README.rst"] = readme
    else:
        commands = parse_body(c.store.load_approval(1).body).done_when_commands
        set_done_when(c, [*commands, "printf 'Heading\\n=======\\n' > README.rst"])
    c.executor.steps.append(resolution)
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.executor.calls) == 2 and c.store.get_run(1).attempts == 2
    assert git_command(c.history.origin, "show", "foragent/issue-1:README.rst") == readme.strip()


@pytest.mark.parametrize("failure", ["markers", "checks"])
def test_conflict_resolution_exhaustion_never_publishes_failed_resolution(conflicting_lanes, failure):
    c = conflicting_lanes
    content = "commands = ['help', 'issue']\n" + ("<<<<<<< HEAD\n" if failure == "markers" else "")
    c.executor.steps += [{"cli.py": content}] * 2
    assert run_issue(1, c.ctx) == "retries-exceeded"
    assert len(c.executor.calls) == 3 and c.store.get_run(1).attempts == 3
    assert c.store.load_flag(1).reason == Reason.RETRIES_EXCEEDED
    assert len(c.ci_heads) == 1 and c.tracker.merged_prs == []
    assert git_command(c.history.origin, "rev-parse", "foragent/issue-1") == c.ci_heads[0]
    assert not any(args == ["merge", "--abort"] for _, args in c.operations)


def test_conflict_resolution_does_not_receive_a_fresh_attempt_budget(conflicting_lanes):
    c = conflicting_lanes
    c.ctx.config = replace(c.ctx.config, max_retries=1)
    c.executor.steps.insert(0, {"cli.py": "commands = ['help']\n"})
    assert run_issue(1, c.ctx) == "retries-exceeded"
    assert len(c.executor.calls) == 2 and c.store.get_run(1).attempts == 2
    assert "Merge conflict with main: cli.py" in c.tracker.list_comments(1)[-1].body
    assert c.tracker.merged_prs == []


def test_fresh_base_done_when_failure_returns_to_implementer(run_case, monkeypatch):
    c = run_case
    c.executor.steps = [{"feature.txt": "done"}, {"parallel.txt": "fixed"}]
    set_done_when(c, [COMMAND, 'test ! -f parallel.txt || test "$(cat parallel.txt)" = fixed'])
    count = []

    def ci(pr):
        count.append(pr)
        if len(count) == 1:
            c.history.update({"parallel.txt": "broken"})
        return "pass"

    monkeypatch.setattr(c.tracker, "pr_checks", ci)
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.executor.calls) == 2
    assert 'Command: test ! -f parallel.txt' in c.executor.calls[1]["prompt"]
    assert c.store.get_run(1).attempts == 2
    assert git_command(c.history.origin, "show", "foragent/issue-1:parallel.txt") == "fixed"


def test_moving_base_times_out_after_three_merges(run_case, monkeypatch):
    c = run_case
    moves = []

    def ci(pr):
        moves.append(pr)
        c.history.update({f"parallel-{len(moves)}.txt": "fresh"})
        return "pass"

    monkeypatch.setattr(c.tracker, "pr_checks", ci)
    assert run_issue(1, c.ctx) == "timeout"
    assert len(moves) == 4
    assert len([args for _, args in c.operations if "--no-edit" in args]) == 3
    assert c.store.load_flag(1).reason == Reason.TIMEOUT
    assert "base kept moving" in c.tracker.list_comments(1)[-1].body
    assert c.tracker.merged_prs == []


def scripted_results(case, monkeypatch, messages, *, log_only=False):
    execute = case.executor.run
    messages = iter(messages)

    def run(**kwargs):
        result = execute(**kwargs)
        message, error_kind = next(messages)
        result = replace(result, exit_code=1 if error_kind != "normal" else result.exit_code, error_kind=error_kind)
        if log_only:
            kwargs["log_path"].parent.mkdir(parents=True, exist_ok=True)
            with kwargs["log_path"].open("a") as stream:
                stream.write(message + "\n")
            return replace(result, last_message="no result")
        return replace(result, last_message=message)

    monkeypatch.setattr(case.executor, "run", run)


@pytest.mark.parametrize("log_only", [False, True])
def test_transients_preserve_attempt_and_budget_and_fallback_persists(run_case, monkeypatch, log_only):
    c = run_case
    c.ctx.config = replace(c.ctx.config, max_runtime_minutes=1, executor_model="primary",
                           executor_fallback_model="fallback")
    c.executor.steps = [{}, {}, {"feature.txt": "wrong"}, {"feature.txt": "done"}]
    scripted_results(c, monkeypatch, [("at capacity", "transient"), ("overloaded", "transient"),
                                     ("done", "normal"), ("done", "normal")], log_only=log_only)
    waits = []

    def sleep(seconds):
        waits.append(seconds)
        c.now[0] += timedelta(seconds=seconds)

    c.ctx.sleep = sleep
    assert run_issue(1, c.ctx) == "merged"
    assert waits == [120, 240]
    assert [call["model"] for call in c.executor.calls] == ["primary", "primary", "fallback", "fallback"]
    assert [call["timeout_s"] for call in c.executor.calls] == [60] * 4
    assert c.executor.calls[0]["prompt"] == c.executor.calls[1]["prompt"] == c.executor.calls[2]["prompt"]
    run = c.store.get_run(1)
    assert run.attempts == 2 and run.waited_seconds == 360
    assert run.fallback_model == "fallback" and run.waiting_until is None
    assert "Executor fallback model: fallback" in c.tracker.created_prs[0].body


def test_transient_budget_exhaustion_is_not_an_implementation_failure(run_case, monkeypatch):
    c = run_case
    c.executor.steps = [{}] * 5
    scripted_results(c, monkeypatch, [("503", "transient")] * 5)
    waits = []
    c.ctx.sleep = lambda seconds: waits.append(seconds) or c.now.__setitem__(0, c.now[0] + timedelta(seconds=seconds))
    assert run_issue(1, c.ctx) == "retries-exceeded"
    assert waits == [120, 240, 480, 960]
    assert c.store.get_run(1).attempts == 1
    assert c.store.get_run(1).waited_seconds == 1800
    assert c.store.get_run(1).executor_waited_seconds == 1800
    assert "Executor unavailable (transient errors)" in c.tracker.list_comments(1)[-1].body
    assert c.tracker.created_prs == []


def test_previous_log_errors_do_not_classify_the_current_execution(run_case):
    c = run_case
    c.store.log_path(1).parent.mkdir(parents=True, exist_ok=True)
    c.store.log_path(1).write_text("503 overloaded usage limit quota\n")
    assert run_issue(1, c.ctx) == "merged"
    assert c.store.get_run(1).waited_seconds == 0


def test_implementer_limit_pauses_lane_and_returns_ready_without_attempt(run_case, monkeypatch):
    c = run_case
    c.store.update_run(1, lane="ui")
    c.ctx.config = replace(c.ctx.config, lanes={"ui": {}}, limit_wait_minutes=15)
    c.executor.steps = [{}]
    scripted_results(c, monkeypatch, [("429 usage limit", "limit")])
    assert run_issue(1, c.ctx) == "limit-wait"
    run = c.store.get_run(1)
    assert run.outcome == "limit-wait" and run.attempts == 1 and run.waited_seconds == 0
    assert c.store.lane_paused("ui", c.now[0] + timedelta(minutes=14))
    assert not c.store.lane_paused("ui", c.now[0] + timedelta(minutes=15))
    assert not c.store.lane_paused("main", c.now[0])
    assert c.tracker.get_issue(1).labels == {"approved", "ready"}
    assert c.store.load_flag(1) is None and c.tracker.created_prs == []


@pytest.mark.parametrize("message", ["overloaded", "429 quota"])
def test_reviewer_availability_waits_then_uses_recovered_verdict(run_case, message):
    c = run_case
    calls = enable_reviewer(c, [ExecResult(1, False, message, Path("unused"),
                                        error_kind="limit" if "429" in message else "transient"), "VERDICT: PASS"])
    assert run_issue(1, c.ctx) == "merged"
    assert len(calls) == 2 and c.store.get_run(1).attempts == 1
    assert c.store.get_run(1).waited_seconds == 120
    assert not c.store.lane_paused("main", c.now[0])
    assert "Round 1: VERDICT: PASS" in c.tracker.created_prs[0].body
    assert "Round 2" not in c.tracker.created_prs[0].body


def test_reviewer_limit_only_becomes_unusable_after_wait_budget(run_case):
    c = run_case
    c.executor.steps.append({})
    c.ctx.config = replace(c.ctx.config, transient_max_wait_minutes=2)
    limit = ExecResult(1, False, "usage limit", Path("unused"), error_kind="limit")
    calls = enable_reviewer(c, [limit, limit, limit])
    assert run_issue(1, c.ctx) == "retries-exceeded"
    assert len(calls) == 3 and c.store.get_run(1).waited_seconds == 120
    assert c.store.get_run(1).attempts == 2
    assert c.store.load_flag(1).reason == Reason.RETRIES_EXCEEDED
    assert not c.store.lane_paused("main", c.now[0])


def test_reviewer_transient_exhaustion_uses_executor_unavailable_flag(run_case):
    c = run_case
    c.ctx.config = replace(c.ctx.config, transient_max_wait_minutes=2)
    transient = ExecResult(1, False, "overloaded", Path("unused"), error_kind="transient")
    calls = enable_reviewer(c, [transient, transient])
    assert run_issue(1, c.ctx) == "retries-exceeded"
    assert len(calls) == 2 and c.store.get_run(1).waited_seconds == 120
    assert c.store.get_run(1).attempts == 1
    assert c.store.load_flag(1).reason == Reason.RETRIES_EXCEEDED
    assert "Executor unavailable (transient errors)" in c.tracker.list_comments(1)[-1].body


def test_lane_matched_paths_are_not_reported_outside(run_case):
    c = run_case
    c.store.update_run(1, lane="ui")
    c.ctx.config = replace(c.ctx.config, lanes={"ui": {"paths": ["**/*.txt"]}})
    assert run_issue(1, c.ctx) == "merged"
    assert "## Outside lane" not in c.tracker.created_prs[0].body


def test_transient_timeout_result_retries_when_runtime_budget_remains(run_case, monkeypatch):
    c = run_case
    c.executor.steps = [{}, {"feature.txt": "done"}]
    execute = c.executor.run

    def run(**kwargs):
        result = execute(**kwargs)
        if len(c.executor.calls) == 1:
            return replace(result, exit_code=-9, timed_out=True, last_message="stream disconnected", error_kind="transient")
        return result

    monkeypatch.setattr(c.executor, "run", run)
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.executor.calls) == 2
    assert c.store.get_run(1).attempts == 1 and c.store.get_run(1).waited_seconds == 120


def test_fresh_base_new_workflow_gets_ci_registration_grace(run_case, monkeypatch):
    c = run_case
    polls = []
    started = c.now[0]

    def checks(pr):
        polls.append(c.now[0])
        if len(polls) == 1:
            c.history.update({".github/workflows/ci.yml": "name: Newly added CI\n"})
        return "none"

    monkeypatch.setattr(c.tracker, "pr_checks", checks)
    assert run_issue(1, c.ctx) == "merged"
    assert polls == [started, *[started + timedelta(seconds=30 * index) for index in range(7)]]
    assert c.now[0] == started + timedelta(minutes=3)


def test_parallel_lanes_implement_while_merge_phase_serializes_and_rechecks(run_case, monkeypatch):
    c = run_case
    c.ctx.config = replace(c.ctx.config, max_retries=0, lanes={"ui": {}, "api": {}})
    set_done_when(c, ["test ! -f a || test ! -f b"])
    approval = c.store.load_approval(1)
    c.tracker.add_issue(2, "Other lane", approval.body, labels=["approved", "running"])
    c.store.save_approval(**{**approval.__dict__, "number": 2})
    c.store.update_run(1, lane="ui")
    c.store.start_run(2, branch="foragent/issue-2", worktree=c.store.worktree_path(2), lane="api")
    c.tracker.set_pr_files(3, ["a"])
    c.tracker.set_pr_files(4, ["b"])
    first_in_ci = threading.Event()
    second_waiting = threading.Event()
    release_first = threading.Event()
    local = threading.local()
    entries = []
    merge_lock = c.store.merge_lock
    merge = c.tracker.merge_pr

    def implement(**kwargs):
        number = 1 if kwargs["worktree"].name == "issue-1" else 2
        local.number = number
        c.executor.calls.append(kwargs)
        (kwargs["worktree"] / ("a" if number == 1 else "b")).write_text("implemented")
        return ExecResult(0, False, "done", kwargs["log_path"])

    @contextmanager
    def observed_lock():
        if local.number == 2:
            second_waiting.set()
        with merge_lock():
            entries.append(local.number)
            yield

    def checks(pr):
        assert entries == [1]
        first_in_ci.set()
        assert release_first.wait(10)
        return "pass"

    def merge_into_target(pr, **kwargs):
        assert entries == [1]
        merge(pr, **kwargs)
        git_command(c.history.origin, "update-ref", "refs/heads/main", kwargs["expected_head_sha"])

    monkeypatch.setattr(c.executor, "run", implement)
    monkeypatch.setattr(c.store, "merge_lock", observed_lock)
    monkeypatch.setattr(c.tracker, "pr_checks", checks)
    monkeypatch.setattr(c.tracker, "merge_pr", merge_into_target)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run_issue, 1, c.ctx)
        try:
            assert first_in_ci.wait(10)
            second = pool.submit(run_issue, 2, c.ctx)
            assert second_waiting.wait(10)
            assert len(c.executor.calls) == 2 and entries == [1]
            assert not second.done()
        finally:
            release_first.set()
        assert first.result(timeout=10) == "merged"
        assert second.result(timeout=10) == "retries-exceeded"
    assert entries == [1, 2]
    assert len(c.tracker.merged_prs) == 1
    assert "Command: test ! -f a || test ! -f b" in c.tracker.list_comments(2)[-1].body
    assert git_command(c.history.origin, "ls-tree", "--name-only", "main") == "a\ntests"
    # Only the first lane was published before CI; the second failed fresh-base verification.
    second_log = c.store.log_path(2).read_text()
    assert second_log.count("Done when command:") == 2


def test_merge_lock_wait_credits_elapsed_time_and_keeps_active_budget(run_case, monkeypatch):
    c = run_case
    c.ctx.config = replace(c.ctx.config, max_runtime_minutes=1)
    merge_lock = c.store.merge_lock
    entries = []

    @contextmanager
    def delayed_lock():
        waiting = c.store.get_run(1)
        assert waiting.waiting_until is not None
        c.now[0] += timedelta(seconds=300)
        with merge_lock():
            entries.append(True)
            yield

    monkeypatch.setattr(c.store, "merge_lock", delayed_lock)
    assert run_issue(1, c.ctx) == "merged"
    run = c.store.get_run(1)
    assert entries == [True] and run.waited_seconds == 300 and run.waiting_until is None
    assert c.store.load_flag(1) is None


@pytest.mark.parametrize("prior_backoff", [False, True])
def test_merge_lock_wait_does_not_consume_conflict_executor_backoff_budget(conflicting_lanes, monkeypatch, prior_backoff):
    c = conflicting_lanes
    c.ctx.config = replace(c.ctx.config, max_runtime_minutes=1)
    c.executor.steps += [{}, {"cli.py": "commands = ['help', 'base', 'issue']\n"}]
    results = [("done", "normal"), ("503", "transient"), ("done", "normal")]
    if prior_backoff:
        c.executor.steps.insert(0, {})
        results.insert(0, ("503", "transient"))
    scripted_results(c, monkeypatch, results)
    merge_lock = c.store.merge_lock
    waits = []

    @contextmanager
    def delayed_lock():
        c.now[0] += timedelta(minutes=31)
        with merge_lock():
            yield

    def sleep(seconds):
        waits.append(seconds)
        c.now[0] += timedelta(seconds=seconds)

    c.ctx.sleep = sleep
    monkeypatch.setattr(c.store, "merge_lock", delayed_lock)
    assert run_issue(1, c.ctx) == "merged"
    assert waits == [120] * (2 if prior_backoff else 1) + [30]
    run = c.store.get_run(1)
    assert run.attempts == 2 and run.executor_waited_seconds == 120 * (2 if prior_backoff else 1)
    assert run.waited_seconds == 31 * 60 + run.executor_waited_seconds
    assert [call["timeout_s"] for call in c.executor.calls] == [60] * (4 if prior_backoff else 3)
    assert c.store.load_flag(1) is None


def test_backoff_oversleep_preserves_runtime_for_next_executor(run_case, monkeypatch):
    c = run_case
    c.ctx.config = replace(c.ctx.config, max_runtime_minutes=1)
    c.executor.steps = [{}, {"feature.txt": "done"}]
    scripted_results(c, monkeypatch, [("HTTP 503", "transient"), ("done", "normal")])
    waits = []

    def oversleep(seconds):
        waits.append(seconds)
        c.now[0] += timedelta(seconds=300)

    c.ctx.sleep = oversleep
    assert run_issue(1, c.ctx) == "merged"
    assert waits == [120]
    assert [call["timeout_s"] for call in c.executor.calls] == [60, 60]
    assert c.store.get_run(1).waited_seconds == 300
    assert c.store.get_run(1).executor_waited_seconds == 300


def test_merge_lock_heartbeat_records_wait_and_clears_it_after_acquisition(tmp_path, monkeypatch):
    now = [BASE_TIME]
    store = StateStore(tmp_path / "state", now=lambda: now[0])
    store.start_run(1, branch="branch", worktree="worktree")
    store.update_run(1, waited_seconds=10, executor_waited_seconds=5)
    ctx = SimpleNamespace(store=store, now=lambda: now[0], monotonic=lambda: (now[0] - BASE_TIME).total_seconds())

    def acquire(execute, poll):
        now[0] += timedelta(minutes=20)
        poll()
        run = store.get_run(1)
        assert run.waited_seconds == 1210 and run.last_progress_at == now[0].isoformat()
        assert run.executor_waited_seconds == 5
        assert run.waiting_until == (now[0] + timedelta(seconds=60)).isoformat()
        return execute()

    monkeypatch.setattr("foragent.runner.poll_executor", acquire)
    with _merge_lock(1, ctx):
        assert store.get_run(1).waiting_until is None
    assert store.get_run(1).waited_seconds == 1210
    assert store.get_run(1).executor_waited_seconds == 5


@pytest.mark.parametrize("elapsed,interrupted", [(70, True), (300, True), (300, False)])
def test_backoff_records_monotonic_elapsed_even_if_interrupted_or_wall_clock_moves(tmp_path, elapsed, interrupted):
    wall = [BASE_TIME]
    mono = [100.0]
    store = StateStore(tmp_path / "state", now=lambda: wall[0])
    store.start_run(1, branch="branch", worktree="worktree")
    store.update_run(1, waited_seconds=10, executor_waited_seconds=5)

    def sleep(seconds):
        assert store.get_run(1).waited_seconds == 130
        assert store.get_run(1).executor_waited_seconds == 125
        mono[0] += elapsed
        wall[0] -= timedelta(hours=1)
        if interrupted:
            raise SystemExit(1)

    ctx = SimpleNamespace(store=store, now=lambda: wall[0], monotonic=lambda: mono[0], sleep=sleep)
    if interrupted:
        with pytest.raises(SystemExit):
            _executor_wait(1, ctx, 120)
    else:
        _executor_wait(1, ctx, 120)
    run = store.get_run(1)
    assert run.waited_seconds == 10 + elapsed
    assert run.executor_waited_seconds == 5 + elapsed
    assert run.waiting_until is None and run.last_progress_at == wall[0].isoformat()


def test_limit_wait_resume_cannot_reset_consumed_implementation_attempt(run_case, monkeypatch):
    c = run_case
    c.executor.steps = [{"feature.txt": "wrong"}, {}, {"feature.txt": "wrong"}]
    scripted_results(c, monkeypatch, [("failed check", "normal"), ("HTTP 429", "limit"), ("failed check", "normal")])
    c.store.update_run(1, restarts=1)
    assert run_issue(1, c.ctx) == "limit-wait"
    assert (c.store.get_run(1).attempts, c.store.get_run(1).restarts) == (2, 1)
    c.now[0] += timedelta(minutes=c.ctx.config.limit_wait_minutes)
    c.store.start_run(1, branch="foragent/issue-1", worktree=c.store.worktree_path(1))
    c.tracker.add_labels(1, ["running"])
    assert run_issue(1, c.ctx) == "retries-exceeded"
    assert len(c.executor.calls) == 3
    assert (c.store.get_run(1).attempts, c.store.get_run(1).restarts) == (2, 1)


@pytest.mark.parametrize("message,success", [
    ("Tests failed: expected quota=10, got quota=0", False),
    ("Implemented quota accounting", True),
    ("HTTP 503 test case for overloaded services", False),
])
def test_implementation_text_does_not_pause_or_backoff(run_case, monkeypatch, message, success):
    c = run_case
    c.executor.exit_code = 0 if success else 1
    c.executor.steps = [{"feature.txt": "done" if success else "wrong"}] * 2
    scripted_results(c, monkeypatch, [(message, "normal")] * 2, log_only=True)
    assert run_issue(1, c.ctx) == ("merged" if success else "retries-exceeded")
    run = c.store.get_run(1)
    assert run.waited_seconds == 0 and run.attempts == (1 if success else 2)
    assert not c.store.lane_paused("main", c.now[0])


def test_review_text_about_limits_uses_normal_review_failure_budget(run_case):
    c = run_case
    c.executor.steps = [{"feature.txt": "done"}] * 2
    calls = enable_reviewer(c, ["VERDICT: FAIL\nfeature.py:503: expected quota=10"] * 2)
    assert run_issue(1, c.ctx) == "retries-exceeded"
    assert len(calls) == 2 and c.store.get_run(1).attempts == 2
    assert c.store.get_run(1).waited_seconds == 0


@pytest.mark.parametrize("size,efforts,tiers", [
    ("s", ["medium", "high", "xhigh", "xhigh"], ["s", "m", "l", "l"]),
    ("m", ["high", "xhigh", "xhigh", "xhigh"], ["m", "l", "l", "l"]),
    ("l", ["xhigh"] * 4, ["l"] * 4),
    (None, ["high", "xhigh", "xhigh", "xhigh"], ["m", "l", "l", "l"]),
])
def test_difficulty_escalates_per_failed_attempt_and_is_recorded(run_case, size, efforts, tiers):
    c = run_case
    if size:
        c.tracker.add_labels(1, [f"size:{size}"])
    c.ctx.config = replace(c.ctx.config, max_retries=3)
    c.executor.steps = [{"feature.txt": "not done"}] * 3 + [{"feature.txt": "done"}]
    assert run_issue(1, c.ctx) == "merged"
    assert [call["effort"] for call in c.executor.calls] == efforts
    run = c.store.get_run(1)
    assert run.attempt_tiers == {str(i + 1): tier for i, tier in enumerate(tiers)}
    assert run.tier == "l"
    assert c.store.read_history()[-1].attempt_tiers == run.attempt_tiers
    for index, tier in enumerate(tiers, 1):
        assert f"Attempt {index} at tier {tier}" in c.tracker.created_prs[0].body


@pytest.mark.parametrize("lane_model,expected", [(None, ["small-model", "medium-model"]),
                                                    ("lane-model", ["lane-model"] * 2)])
def test_claude_tier_model_and_lane_precedence(run_case, lane_model, expected):
    c = run_case
    c.tracker.add_labels(1, ["size:s", "lane:api"])
    c.store.update_run(1, lane="api")
    tiers = {"s": {"codex_effort": "medium", "claude_model": "small-model"},
             "m": {"codex_effort": "high", "claude_model": "medium-model"},
             "l": {"codex_effort": "xhigh", "claude_model": "large-model"}}
    c.ctx.config = replace(c.ctx.config, executor="codex", executor_model="global-model", tiers=tiers,
                           lanes={"api": {"executor": "claude", "model": lane_model}})
    kinds = []
    c.ctx.executor_factory = lambda name: kinds.append(name) or c.executor
    c.executor.steps = [{"feature.txt": "wrong"}, {"feature.txt": "done"}]
    assert run_issue(1, c.ctx) == "merged"
    assert kinds == ["claude"]
    assert [call["model"] for call in c.executor.calls] == expected
    assert all("effort" not in call for call in c.executor.calls)
