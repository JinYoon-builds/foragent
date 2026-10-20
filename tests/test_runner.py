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
from foragent.runner import CheckResult, RunContext, build_prompt, run_issue
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
            git_command(git_history.origin, "fetch", str(root), f"+refs/heads/{branch}:refs/heads/{branch}")
            tracker.set_pr_head_sha(2, git_command(root, "rev-parse", "HEAD"))
            return ""
        return subprocess.run(
            ["git", *args], cwd=root, check=True, capture_output=True, text=True,
        ).stdout

    context = RunContext(
        repo="owner/repo", repo_root=git_history.clone, tracker=tracker, store=store,
        config=Config(review="fast", max_retries=1), git=GitRepo(git_history.clone, git_run),
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
    assert not store.worktree_path(1).exists()
    assert str(store.worktree_path(1)) not in git_command(git_history.clone, "worktree", "list")
    assert len(finished) == 1
    assert len(store.read_history()) == 1
    assert store.get_run(1).status == "finished"
    assert "running" not in tracker.get_issue(1).labels


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
    assert "not implemented yet; PR left open" in c.tracker.list_comments(1)[-1].body


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
