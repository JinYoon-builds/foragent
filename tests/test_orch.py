"""Orchestrator recovery must preserve evidence and a visible next action."""

from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path

import pytest

from conftest import ACCEPTANCE, git_command
from test_runner import enable_reviewer, run_case
from foragent import cli, orch, runner
from foragent.executors import ExecResult
from foragent.issue import body_hash
from foragent.reasons import Reason


@pytest.fixture
def review_case(run_case):
    c = run_case
    c.ctx.config = replace(c.ctx.config, repo="owner/repo")
    c.store.finish_run(1, outcome="blocked")
    c.now[0] += timedelta(seconds=1)
    c.ctx.tracker.remove_labels(1, ["running"])
    c.ctx.tracker.add_labels(1, ["blocked"])
    c.ctx.tracker.comment(1, "<!-- foragent:blocked reason=review-dispute -->\nOriginal details")
    base = c.history.tip
    c.history.update({"feature.txt": "done"})
    c.head = c.history.tip
    c.pr = c.tracker.create_pr(head="foragent/issue-1", base="main", title="Existing PR", body="Keep this PR body")
    c.tracker.set_pr_head_sha(c.pr, c.head)
    git_command(c.history.origin, "update-ref", "refs/heads/foragent/issue-1", c.head)
    git_command(c.history.origin, "update-ref", "refs/heads/main", base)
    c.ctx.git.fetch()
    c.review_calls = enable_reviewer(c, ["VERDICT: PASS"])
    yield c
    assert c.executor.calls == []
    assert not any(e.label == "approved" and e.action == "labeled" and e.actor != "human"
                   for e in c.tracker.label_events(1))
    assert not c.store.worktree_path(1).exists()
    assert c.store.get_run(1).status == "finished"
    if c.tracker.get_issue(1).state == "open":
        assert c.tracker.get_issue(1).labels & {"blocked", "ready", "running"}


def assert_blocked(c, reason):
    assert c.tracker.merged_prs == []
    assert c.tracker.get_issue(1).labels & {"blocked", "ready", "running", "needs-human"} == {"blocked"}
    assert c.store.load_flag(1).reason == reason
    assert c.tracker.get_issue(c.pr).state == "open"
    assert len(c.tracker.created_prs) == 1


def test_review_pending_close_finishes_without_failure_flag(review_case):
    c = review_case
    c.ctx.tracker.remove_labels(1, ["blocked"])
    c.ctx.tracker.refresh_issues()
    c.store.queue_github_write(1, "comment", body="Pending evidence")

    assert runner.review_only(1, c.ctx) == "merged"

    assert c.tracker.merged_prs == [c.pr]
    assert c.tracker.get_issue(1).state == "open"
    row, = c.store.local_issues()
    assert row["state"] == "closed"
    assert not set(row["labels"]) & {"blocked", "ready", "running", "needs-human"}
    assert c.store.load_flag(1) is None
    c.ctx.tracker.flush_outbox()
    assert c.tracker.get_issue(1).state == "closed"
    assert not c.tracker.get_issue(1).labels & {"blocked", "ready", "running", "needs-human"}
    assert not any("retries-exceeded" in comment.body for comment in c.tracker.list_comments(1))
    assert c.store.github_outbox() == []


@pytest.mark.parametrize("entry", ["review-only", "resolve"])
def test_fast_review_merge_reuses_post_merge_reinstall(review_case, monkeypatch, entry):
    c = review_case
    calls = []

    def reinstall(ctx, pr, base):
        assert c.tracker.merged_prs == [c.pr]
        calls.append((ctx, pr, base))

    monkeypatch.setattr(runner, "after_merge", reinstall)
    if entry == "resolve":
        orch.resolve(c.ctx, 1, "pass", "Reviewed the current head")
    else:
        assert runner.review_only(1, c.ctx) == "merged"
    assert calls == [(c.ctx, c.pr, "main")]


def test_failed_review_merge_does_not_reinstall(review_case, monkeypatch):
    c = review_case
    calls = []

    def failed_merge(*args, **kwargs):
        raise runner.TrackerError("merge failed")

    monkeypatch.setattr(c.tracker, "merge_pr", failed_merge)
    monkeypatch.setattr(runner, "after_merge", lambda *args: calls.append(args))
    assert runner.review_only(1, c.ctx) == "error"
    assert_blocked(c, Reason.RETRIES_EXCEEDED)
    assert "merge failed" in c.store.log_path(1).read_text()
    assert calls == []


@pytest.mark.parametrize("entry,operation", [
    ("review-only", "get_issue"), ("review-only", "pr_checks"),
    ("rerun-review", "pr_checks"), ("resolve", "merge_pr"),
])
def test_review_rate_limit_remains_a_local_wait_including_audit_cleanup(review_case, monkeypatch, entry, operation):
    c = review_case
    until = c.now[0] + timedelta(minutes=10)
    failed = False
    followups = []

    def limited(*args, **kwargs):
        nonlocal failed
        failed = True
        raise runner.TrackerError("API rate limit exceeded (HTTP 403)",
                                  stdout=f"X-RateLimit-Reset: {int(until.timestamp())}\r\n")

    with monkeypatch.context() as patch:
        for method in ("get_issue", "pr_head_sha", "add_labels", "remove_labels", "comment"):
            original = getattr(c.tracker, method)

            def tracked(*args, _method=method, _original=original, **kwargs):
                if failed:
                    followups.append(_method)
                return _original(*args, **kwargs)

            patch.setattr(c.tracker, method, tracked)
        patch.setattr(c.tracker, operation, limited)
        if entry == "review-only":
            assert runner.review_only(1, c.ctx) == "limit-wait"
        elif entry == "rerun-review":
            assert "limit-wait" in orch.rerun_review(c.ctx, 1)
        else:
            assert "limit-wait" in orch.resolve(c.ctx, 1, "pass", "Reviewed this head")
        assert followups == []
    assert failed
    assert c.store.get_run(1).waiting_until == until.isoformat()
    assert c.store.get_run(1).outcome == "limit-wait"
    assert c.store.load_flag(1) is None


def test_resolve_queues_ci_repair_without_changing_the_verdict_head(review_case):
    c = review_case
    logs = "FAILED existing tests: preserve the original failure output"
    c.tracker.set_pr_checks(c.pr, "fail")
    c.tracker.set_pr_failed_logs(c.pr, logs)

    summary = orch.resolve(c.ctx, 1, "pass", "Reviewed the current head")

    assert "ci-failed; queued implementation retry" in summary
    assert c.executor.calls == c.review_calls == []
    assert c.tracker.get_issue(1).labels == {"approved", "ready"}
    assert c.tracker.get_issue(c.pr).state == "closed"
    assert c.tracker.merged_prs == []
    assert c.tracker.pr_head_sha(c.pr) == c.head
    assert not any("push" in args or "commit" in args for _, args in c.operations)
    hint = (c.store.root / "hints/1.txt").read_text()
    assert "Reviewed the current head" in hint
    assert str(c.store.log_path(1)) in hint
    assert logs in c.store.log_path(1).read_text()
    assert any(f"head={c.head} verdict=pass" in comment.body
               for comment in c.tracker.list_comments(1))


@pytest.mark.parametrize("resolved", [False, True])
@pytest.mark.parametrize("repair_result", ["pass", "ci-fail", "review-fail"])
def test_review_ci_failure_repairs_same_pr_and_rechecks_new_head(run_case, monkeypatch, resolved, repair_result):
    c = run_case
    c.ctx.config = replace(c.ctx.config, repo="owner/repo", test_command="make check")
    c.store.finish_run(1, outcome="blocked")
    c.now[0] += timedelta(seconds=1)
    c.tracker.remove_labels(1, ["running"])
    c.tracker.add_labels(1, ["blocked"])
    base = c.history.tip
    c.history.update({"feature.txt": "done", "existing.txt": "preserve existing PR work"})
    head = c.history.tip
    pr = c.tracker.create_pr(head="foragent/issue-1", base="main", title="Existing PR", body="Original")
    c.tracker.set_pr_head_sha(pr, head)
    c.tracker.set_pr_checks(pr, "fail")
    c.tracker.set_pr_failed_logs(pr, "original failed CI output")
    git_command(c.history.origin, "update-ref", "refs/heads/foragent/issue-1", head)
    git_command(c.history.origin, "update-ref", "refs/heads/main", base)
    prior_review = c.store.root / "runs/issue-1/review-1.txt"
    prior_review.parent.mkdir(parents=True)
    prior_review.write_text("prior raw review")
    messages = ([] if resolved else ["VERDICT: PASS"]) + [
        "VERDICT: FAIL\nrepair.txt:1 defect" if repair_result == "review-fail" else "VERDICT: PASS",
    ]
    reviews = enable_reviewer(c, messages)
    c.executor.steps = [{"repair.txt": "CI repair"}]
    execute = c.executor.run

    def implement(**kwargs):
        assert (kwargs["worktree"] / "existing.txt").read_text() == "preserve existing PR work"
        assert c.ctx.git.at(kwargs["worktree"]).head_sha() == head
        c.tracker.set_pr_checks(pr, "fail" if repair_result == "ci-fail" else "pass")
        return execute(**kwargs)

    monkeypatch.setattr(c.executor, "run", implement)
    outcome = runner.review_only(1, c.ctx, verdict_head=head if resolved else None)
    assert outcome == {"pass": "merged", "ci-fail": "ci-failed", "review-fail": "retries-exceeded"}[repair_result]
    assert len(c.executor.calls) == 1
    assert "CI failed:\noriginal failed CI output" in c.executor.calls[0]["prompt"]
    assert "`make check`" in c.executor.calls[0]["prompt"]
    assert len(reviews) == (1 if resolved else 2)
    assert c.store.get_run(1).attempts == 2
    assert prior_review.read_text() == "prior raw review"
    assert len(list(prior_review.parent.glob("review-*.txt"))) == len(reviews) + 1
    assert len(c.tracker.created_prs) == 1
    adds = [args for _, args in c.operations
            if args[:2] == ["worktree", "add"] and str(c.store.worktree_path(1)) in args]
    assert len(adds) == 1 and "--detach" in adds[0]
    assert c.store.log_path(1).read_text().count("Done when command:") == 4
    if repair_result == "pass":
        assert c.tracker.merged_prs == [pr]
        assert c.tracker.pr_head_sha(pr) != head
        assert not c.tracker.get_issue(1).labels & {"blocked", "needs-human"}
    else:
        assert c.tracker.merged_prs == []
        assert c.tracker.get_issue(1).labels & {"blocked", "needs-human"} == {"blocked"}


@pytest.mark.parametrize("entry", ["review-only", "rerun-review", "resolve"])
def test_review_entries_merge_verified_head_without_implementation(review_case, entry):
    c = review_case
    if entry == "resolve":
        orch.resolve(c.ctx, 1, "pass", "Reviewed the current head")
        assert c.review_calls == []
    elif entry == "rerun-review":
        orch.rerun_review(c.ctx, 1)
    else:
        assert runner.review_only(1, c.ctx) == "merged"
    assert c.tracker.merged_prs == [c.pr]
    assert c.tracker.get_issue(1).state == "closed"
    assert not c.tracker.get_issue(1).labels & {"blocked", "ready", "running", "needs-human"}
    assert c.tracker.get_issue(c.pr).body == "Keep this PR body"
    assert len(c.tracker.created_prs) == 1
    assert not any("push" in args or "commit" in args for _, args in c.operations)
    assert git_command(c.history.origin, "rev-parse", "refs/heads/foragent/issue-1") == c.head


@pytest.mark.parametrize("message,outcome,reason", [
    ("VERDICT: FAIL\nfeature.txt:1 broken", "review-failed", Reason.RETRIES_EXCEEDED),
    ("Ambiguous reviewer output", "review-failed", Reason.RETRIES_EXCEEDED),
    (ExecResult(1, False, "review process failed", Path("unused")), "review-failed", Reason.RETRIES_EXCEEDED),
    (ExecResult(0, True, "review timed out", Path("unused")), "timeout", Reason.TIMEOUT),
])
def test_failed_rerun_preserves_pr_and_raw_review_with_visible_reason(review_case, message, outcome, reason):
    c = review_case
    enable_reviewer(c, [message])
    original = c.store.root / "runs/issue-1/review-1.txt"
    original.parent.mkdir(parents=True)
    original.write_text("original raw review")
    assert outcome in orch.rerun_review(c.ctx, 1)
    assert_blocked(c, reason)
    assert original.read_text() == "original raw review"
    assert (original.parent / "review-2.txt").read_text() == (message if isinstance(message, str) else message.last_message)


@pytest.mark.parametrize("entry", ["rerun-review", "resolve"])
@pytest.mark.parametrize("failure,reason", [
    ("checks", Reason.RETRIES_EXCEEDED), ("ci", Reason.RETRIES_EXCEEDED),
    ("ci-timeout", Reason.TIMEOUT), ("check-timeout", Reason.TIMEOUT),
    ("base", Reason.RISK_HIGH), ("head", Reason.RISK_HIGH),
    ("body", Reason.HASH_MISMATCH), ("acceptance", Reason.HASH_MISMATCH),
    ("missing-approval", Reason.HASH_MISMATCH), ("no-check", Reason.NO_MACHINE_CHECK),
    ("error", Reason.RETRIES_EXCEEDED),
])
def test_all_failure_exits_are_visible_and_never_merge(review_case, monkeypatch, entry, failure, reason):
    c = review_case
    if failure == "checks":
        monkeypatch.setattr(runner, "_check", lambda command, *args, **kwargs: runner.CheckResult(command, 1, False, "Broken feature"))
    elif failure == "check-timeout":
        monkeypatch.setattr(runner, "_check", lambda command, *args, **kwargs: runner.CheckResult(command, None, True, "Timed out"))
    elif failure == "ci":
        c.ctx.config = replace(c.ctx.config, max_retries=0)
        c.tracker.set_pr_checks(c.pr, "fail")
    elif failure == "ci-timeout":
        c.tracker.set_pr_checks(c.pr, "pending")
    elif failure == "base":
        c.history.update({"new-base.txt": "base moved"})
    elif failure == "head":
        monkeypatch.setattr(c.tracker, "pr_checks", lambda pr: c.tracker.set_pr_head_sha(pr, "f" * 40) or "pass")
    elif failure == "body":
        c.tracker.edit_body(1, c.tracker.get_issue(1).body + "\nChanged requirements")
    elif failure == "acceptance":
        c.history.update({ACCEPTANCE: "changed acceptance"})
    elif failure == "missing-approval":
        c.store.clear_approval(1)
    elif failure == "no-check":
        approval = c.store.load_approval(1)
        body = "<!-- foragent:v1 -->\n## Goal\nFeature\n## Out of scope\nNone\n## Done when\n- [ ] Looks right\n"
        c.tracker.edit_body(1, body)
        c.store.save_approval(**{**approval.__dict__, "body": body, "body_sha": body_hash(body)})
    elif failure == "error":
        monkeypatch.setattr(runner, "_check", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("check crashed")))

    if entry == "rerun-review":
        orch.rerun_review(c.ctx, 1)
    else:
        orch.resolve(c.ctx, 1, "pass", "Reviewed, still require checks and CI")
    if entry == "resolve" and failure in {"checks", "ci"}:
        assert c.tracker.get_issue(1).labels & {"blocked", "ready", "running"} == {"ready"}
        assert c.tracker.get_issue(c.pr).state == "closed"
        hint = (c.store.root / "hints/1.txt").read_text()
        assert "Verification:" in hint
        if failure == "checks":  # #420: the next attempt reads the saved failing commands
            from foragent.run_previous import saved_failures_section
            assert "Broken feature" in saved_failures_section(c.store.root, 1)
    else:
        assert_blocked(c, reason)
    assert c.tracker.merged_prs == []
    events = [json.loads(line) for line in (c.store.root / "orch-events.jsonl").read_text().splitlines()]
    assert events[-1]["kind"] == entry
    assert any(comment.body == "<!-- foragent:orch -->\n" + events[-1]["summary"]
               for comment in c.tracker.list_comments(1))


@pytest.mark.parametrize("change", ["base", "head", "body", "approval", "risk"])
def test_rechecks_evidence_after_ci(review_case, monkeypatch, change):
    c = review_case
    def ci(pr):
        if change == "base":
            c.history.update({"moved.txt": "new base"})
        elif change == "head":
            c.tracker.set_pr_head_sha(pr, "a" * 40)
        elif change == "body":
            c.tracker.edit_body(1, c.tracker.get_issue(1).body + "\nnew goal")
        elif change == "approval":
            c.tracker.remove_labels(1, ["approved"])
        else:
            c.tracker.add_labels(1, ["risk:high"])
        return "pass"
    monkeypatch.setattr(c.tracker, "pr_checks", ci)
    orch.resolve(c.ctx, 1, "pass", "Head-specific review")
    assert_blocked(c, Reason.HASH_MISMATCH if change in {"body", "approval"} else Reason.RISK_HIGH)


@pytest.mark.parametrize("mode,always_human,risk", [("careful", [], False), ("batch", [], False),
                                                       ("fast", ["feature.txt"], False), ("fast", [], True)])
def test_resolve_preserves_normal_merge_policy(review_case, mode, always_human, risk):
    c = review_case
    c.ctx.config = replace(c.ctx.config, review=mode, always_human=always_human)
    if risk:
        c.tracker.add_labels(1, ["risk:high"])
    orch.resolve(c.ctx, 1, "pass", "Reviewed")
    assert_blocked(c, Reason.ALWAYS_HUMAN_PATH if always_human else Reason.RISK_HIGH)


@pytest.mark.parametrize("entry", ["review-only", "rerun-review", "resolve"])
def test_interruption_leaves_visible_state_and_cleans_worktree(review_case, monkeypatch, entry):
    c = review_case
    def interrupted(*args, **kwargs):
        raise SystemExit(1)
    monkeypatch.setattr(runner, "_check", interrupted)
    with pytest.raises(SystemExit):
        if entry == "resolve":
            orch.resolve(c.ctx, 1, "pass", "Reviewed")
        elif entry == "rerun-review":
            orch.rerun_review(c.ctx, 1)
        else:
            runner.review_only(1, c.ctx)
    assert_blocked(c, Reason.RETRIES_EXCEEDED)


@pytest.mark.parametrize("entry", ["review-only", "rerun-review", "resolve"])
def test_cleanup_failure_still_finishes_run_and_preserves_reason(review_case, monkeypatch, entry):
    c = review_case
    enable_reviewer(c, ["VERDICT: FAIL\nfeature.txt:1 broken"])
    if entry == "resolve":
        c.tracker.set_pr_checks(c.pr, "pending")
    remove = c.ctx.git.remove_worktree

    def cleanup(path):
        existed = path.exists()
        remove(path)
        if existed:
            raise RuntimeError("worktree cleanup failed")

    monkeypatch.setattr(c.ctx.git, "remove_worktree", cleanup)
    with pytest.raises(RuntimeError, match="worktree cleanup failed"):
        if entry == "resolve":
            orch.resolve(c.ctx, 1, "pass", "Reviewed")
        elif entry == "rerun-review":
            orch.rerun_review(c.ctx, 1)
        else:
            runner.review_only(1, c.ctx)
    assert_blocked(c, Reason.TIMEOUT if entry == "resolve" else Reason.RETRIES_EXCEEDED)


@pytest.mark.parametrize("previous_run", [False, True])
def test_missing_pr_remains_visible_with_or_without_run_record(review_case, previous_run):
    c = review_case
    if not previous_run:
        (c.store.root / "runs/1.json").unlink()
    c.tracker.close_pr(c.pr)
    assert runner.review_only(1, c.ctx) == "no-pr"
    assert c.tracker.get_issue(1).labels & {"blocked", "ready", "running"} == {"blocked"}


@pytest.mark.parametrize("failure", ["head-acceptance", "head-before-review", "runtime"])
def test_rerun_early_verification_failures_remain_blocked(review_case, monkeypatch, failure):
    c = review_case
    if failure == "runtime":
        monkeypatch.setattr(runner, "_remaining", lambda *args: 0)
        reason = Reason.TIMEOUT
    elif failure == "head-before-review":
        c.tracker.set_pr_head_sha(c.pr, "f" * 40)
        reason = Reason.RISK_HIGH
    else:
        base = git_command(c.history.origin, "rev-parse", "refs/heads/main")
        c.history.update({ACCEPTANCE: "changed on PR head"})
        c.tracker.set_pr_head_sha(c.pr, c.history.tip)
        git_command(c.history.origin, "update-ref", "refs/heads/foragent/issue-1", c.history.tip)
        git_command(c.history.origin, "update-ref", "refs/heads/main", base)
        reason = Reason.HASH_MISMATCH
    orch.rerun_review(c.ctx, 1)
    assert c.review_calls == []
    assert_blocked(c, reason)


@pytest.mark.parametrize("action", ["retry", "rerun_review", "resolve", "escalate"])
def test_running_work_cannot_be_overwritten(review_case, action):
    c = review_case
    c.tracker.add_labels(1, ["running"])
    args = {"retry": (), "rerun_review": (), "resolve": ("fail", "note"), "escalate": ("summary", "tried")}
    with pytest.raises(orch.OrchError, match="running"):
        getattr(orch, action)(c.ctx, 1, *args[action])
    c.tracker.remove_labels(1, ["running"])
    assert c.tracker.get_issue(c.pr).state == "open"


def test_escalation_options_and_immutable_approval(review_case):
    c = review_case
    approval = c.store.load_approval(1)
    with pytest.raises(orch.OrchError, match="1-based"):
        orch.escalate(c.ctx, 1, "Choose locale", "Reviewed", ["English", "Korean"], 0)
    assert "needs-human" not in c.tracker.get_issue(1).labels
    orch.escalate(c.ctx, 1, "Choose locale", "Reviewed twice", ["English", "Korean"], 2)
    text = next(comment.body for comment in c.tracker.list_comments(1)
                if comment.body.startswith("<!-- foragent:needs-human reason=escalated -->"))
    assert "2. Korean (recommended)" in text
    assert c.store.load_approval(1) == approval
    assert "needs-human" in c.tracker.get_issue(1).labels


def test_list_uses_latest_unknown_reason_and_only_last_sixty_lines(review_case):
    c = review_case
    c.now[0] += timedelta(seconds=5)
    c.ctx.tracker.comment(1, "<!-- foragent:blocked reason=future-code -->\r\nNewest details")
    log = c.store.log_path(1)
    log.parent.mkdir(parents=True)
    log.write_text("".join(f"line {i}\n" for i in range(100)))
    item, = orch.list_blocked(c.ctx)
    assert item["reason"] == "future-code"
    assert item["blocked_at"] == c.now[0].isoformat()
    assert item["log_tail"].splitlines() == [f"line {i}" for i in range(40, 100)]
    assert str(log) == item["log_path"]
    assert "Newest details" in item["details"]


def test_inspection_audits_follow_spec_without_resetting_blocked_evidence(review_case):
    c = review_case
    original = c.tracker.get_issue(1)
    blocked_at = c.now[0].isoformat()
    for _ in range(2):
        c.now[0] += timedelta(minutes=1)
        item, = orch.list_blocked(c.ctx)
        assert item["blocked_at"] == blocked_at
        assert item["details"].endswith("Original details")
    prior_events = orch.log(c.ctx)
    events = [json.loads(line) for line in (c.store.root / "orch-events.jsonl").read_text().splitlines()]
    assert [event["kind"] for event in events] == ["list", "list", "log"]
    assert prior_events == events[:-1]
    assert all(event["issue"] is None for event in events)
    comments = [comment.body for comment in c.tracker.list_comments(1)
                if comment.body.startswith("<!-- foragent:orch -->")]
    assert comments == []  # Observation is audited locally and stays offline.
    assert c.tracker.get_issue(1) == original


@pytest.mark.parametrize("native", [False, True])
def test_hint_keeps_native_goal_and_is_consumed_once(run_case, native):
    c = run_case
    c.ctx.config = replace(c.ctx.config, executor="claude" if native else "codex")
    hint = c.store.root / "hints/1.txt"
    hint.parent.mkdir()
    hint.write_text("Use existing locale")
    c.executor.steps = [{"draft.txt": "draft"}, {"feature.txt": "done"}]
    assert runner.run_issue(1, c.ctx) == "merged"
    first, second = [call["prompt"] for call in c.executor.calls]
    assert "Orchestrator hint:\nUse existing locale" in first
    assert first.startswith("Orchestrator hint:")
    if native:
        assert c.executor.calls[0]["goal"] == runner.goal_condition(c.ctx.config.acceptance_dir)
    assert "Use existing locale" not in second
    assert not hint.exists()


@pytest.mark.parametrize("entry", ["retry", "resolve-fail"])
@pytest.mark.parametrize("failure", ["limit", "transient", "transient-timeout", "missing-executor", "crash", "interrupted"])
def test_hint_survives_unavailable_or_interrupted_executor_until_next_implementation(run_case, monkeypatch, entry, failure):
    c = run_case
    c.store.finish_run(1, outcome="blocked")
    c.now[0] += timedelta(seconds=1)
    c.tracker.remove_labels(1, ["running"])
    c.tracker.add_labels(1, ["blocked"])
    hint = "기존 언어 선택을 사용하세요"
    if entry == "resolve-fail":
        c.tracker.create_pr(head="foragent/issue-1", base="main", title="Old attempt", body="Old")
        orch.resolve(c.ctx, 1, "fail", hint)
    else:
        orch.retry(c.ctx, 1, hint)
    path = c.store.root / "hints/1.txt"
    execute = c.executor.run
    calls = []
    c.ctx.config = replace(c.ctx.config, transient_max_wait_minutes=1)

    def unavailable(**kwargs):
        calls.append(kwargs)
        if failure == "missing-executor":
            raise FileNotFoundError("executor not installed")
        if failure == "crash":
            raise RuntimeError("executor crashed")
        if failure == "interrupted":
            raise SystemExit(143)
        if failure == "transient-timeout":
            c.now[0] += timedelta(minutes=c.ctx.config.max_runtime_minutes + 1)
        return ExecResult(1, False, "Executor unavailable", kwargs["log_path"],
                          error_kind="limit" if failure == "limit" else "transient")

    monkeypatch.setattr(c.executor, "run", unavailable)
    c.store.start_run(1, branch="foragent/issue-1", worktree=c.store.worktree_path(1))
    c.tracker.add_labels(1, ["running"])
    c.tracker.remove_labels(1, ["ready"])
    if failure == "interrupted":
        with pytest.raises(SystemExit):
            runner.run_issue(1, c.ctx)
    else:
        outcome = runner.run_issue(1, c.ctx)
        assert outcome == {"limit": "limit-wait", "transient": "retries-exceeded",
                           "transient-timeout": "timeout"}.get(failure, "error")
    assert calls and all("Orchestrator hint:\n" + hint in call["prompt"] for call in calls)
    assert c.tracker.merged_prs == []
    assert path.read_text() == hint

    # Resume after availability or crash recovery, without issuing a new hint.
    monkeypatch.setattr(c.executor, "run", execute)
    c.now[0] += timedelta(minutes=c.ctx.config.limit_wait_minutes + 1)
    c.store.start_run(1, branch="foragent/issue-1", worktree=c.store.worktree_path(1))
    c.tracker.add_labels(1, ["running"])
    c.tracker.remove_labels(1, ["blocked", "ready"])
    c.executor.steps = [{"draft.txt": "draft"}, {"feature.txt": "done"}]
    assert runner.run_issue(1, c.ctx) == "merged"
    first, second = [call["prompt"] for call in c.executor.calls]
    assert "Orchestrator hint:\n" + hint in first
    assert hint not in second
    assert not path.exists()


def test_cli_routes_all_verbs_to_injected_context(review_case, monkeypatch, capsys):
    c = review_case
    monkeypatch.chdir(c.ctx.repo_root)
    monkeypatch.setattr(cli, "load_config", lambda root: c.ctx.config)
    monkeypatch.setattr(cli, "GhTracker", lambda repo: c.tracker)
    monkeypatch.setattr(cli, "GitRepo", lambda root: c.ctx.git)
    monkeypatch.setattr(cli.StateStore, "for_repo", lambda repo: c.store)
    monkeypatch.setattr(cli, "RunContext", lambda **kwargs: c.ctx)
    assert cli.main(["orch", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["number"] == 1
    assert cli.main(["orch", "resolve", "1", "--verdict", "fail", "--note", "Fix locale"]) == 0
    assert "queued implementation retry" in capsys.readouterr().out
    assert c.tracker.get_issue(1).labels & {"blocked", "ready"} == {"ready"}
    assert cli.main(["orch", "log"]) == 0
    assert "resolved review fail" in capsys.readouterr().out


@pytest.mark.parametrize("entry", ["review-only", "resolve"])
def test_review_entries_hold_unlisted_harness_change_before_measuring_head(review_case, monkeypatch, entry):
    assert_review_harness_hold(review_case, monkeypatch, entry, "tests/sub/conftest.py")


@pytest.mark.parametrize("entry", ["review-only", "resolve"])
def test_review_hold_sees_head_paths_the_runner_index_omits(review_case, monkeypatch, entry):
    """The review worktree's staging drops .foragent/; the measured head still has it."""
    assert_review_harness_hold(review_case, monkeypatch, entry, ".foragent/conftest.py")


def assert_review_harness_hold(c, monkeypatch, entry, path):
    base = git_command(c.history.origin, "rev-parse", "refs/heads/main")
    c.history.update({path: "# changed harness\n"})
    c.head = c.history.tip
    c.tracker.set_pr_head_sha(c.pr, c.head)
    git_command(c.history.origin, "update-ref", "refs/heads/foragent/issue-1", c.head)
    git_command(c.history.origin, "update-ref", "refs/heads/main", base)
    c.ctx.git.fetch()
    measured = []
    monkeypatch.setattr(runner, "_check", lambda *args, **kwargs: measured.append(args))
    if entry == "resolve":
        orch.resolve(c.ctx, 1, "pass", "Reviewed the current head")
    else:
        assert runner.review_only(1, c.ctx) == "held"
    assert measured == [] and c.review_calls == []
    assert c.tracker.merged_prs == []
    assert c.tracker.get_issue(c.pr).state == "open"
    assert {"blocked", "needs-human"} <= c.tracker.get_issue(1).labels
    assert c.store.load_flag(1).reason == Reason.TEST_HARNESS_CHANGED
    assert any(path in comment.body for comment in c.tracker.list_comments(1)
               if "reason=test-harness-changed" in comment.body)


def test_review_only_spec_conflict_stops_with_spec_question_and_keeps_pr_open(review_case):
    c = review_case
    conflict = f"SPEC-CONFLICT: {ACCEPTANCE}:1 requires X; the finding wants Y"
    c.review_calls = enable_reviewer(c, [f"VERDICT: PASS\n{conflict}"])

    assert runner.review_only(1, c.ctx) == "spec-question"

    assert_blocked(c, Reason.SPEC_QUESTION)
    question = [comment.body for comment in c.tracker.list_comments(1)
                if comment.body.startswith("<!-- foragent:blocked reason=spec-question -->")]
    assert len(question) == 1 and conflict in question[0]
    events = [json.loads(line) for line in (c.store.root / "orch-events.jsonl").read_text().splitlines()]
    assert [e["verdict"] for e in events if e["kind"] == "review"] == ["SPEC-CONFLICT"]


def test_review_done_when_that_leaves_a_fifo_pyproject_is_held_not_a_staging_error(review_case):
    """The last measurement's leftovers are guarded before the final staging, which Git would refuse."""
    from runner_support import set_done_when

    c = review_case
    c.history.update({"pyproject.toml": '[tool.pytest.ini_options]\naddopts = "-q"\n'})
    c.head = c.history.tip
    c.tracker.set_pr_head_sha(c.pr, c.head)
    git_command(c.history.origin, "update-ref", "refs/heads/foragent/issue-1", c.head)
    git_command(c.history.origin, "update-ref", "refs/heads/main", c.head)
    c.ctx.git.fetch()
    set_done_when(c, ["rm pyproject.toml && mkfifo pyproject.toml"])
    c.tracker.edit_body(1, c.store.load_approval(1).body)

    assert runner.review_only(1, c.ctx) == "held"
    assert c.store.load_flag(1).reason == Reason.TEST_HARNESS_CHANGED
    assert {"blocked", "needs-human"} <= c.tracker.get_issue(1).labels
    assert c.tracker.merged_prs == [] and c.tracker.get_issue(c.pr).state == "open"


def test_review_fifo_between_done_when_commands_is_held_before_staging(review_case):
    """The guard before the second measurement decides before it stages the FIFO."""
    from runner_support import set_done_when

    c = review_case
    c.history.update({"pyproject.toml": '[tool.pytest.ini_options]\naddopts = "-q"\n'})
    c.head = c.history.tip
    c.tracker.set_pr_head_sha(c.pr, c.head)
    git_command(c.history.origin, "update-ref", "refs/heads/foragent/issue-1", c.head)
    git_command(c.history.origin, "update-ref", "refs/heads/main", c.head)
    c.ctx.git.fetch()
    set_done_when(c, ["rm pyproject.toml && mkfifo pyproject.toml", "true"])
    c.tracker.edit_body(1, c.store.load_approval(1).body)

    assert runner.review_only(1, c.ctx) == "held"
    assert c.store.load_flag(1).reason == Reason.TEST_HARNESS_CHANGED
    assert {"blocked", "needs-human"} <= c.tracker.get_issue(1).labels
