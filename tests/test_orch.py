"""Recovery must retain the PR, approval and independent verification boundaries."""

from dataclasses import replace
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest

from conftest import ACCEPTANCE, BASE_TIME, git_command
from foragent import orch
from foragent.config import Config
from foragent.executors import ExecResult
from foragent.git import GitRepo
from foragent.issue import body_hash, parse_body
from foragent.runner import RunContext, build_prompt, review_only
from foragent.state import StateStore
from foragent.tracker import FakeTracker


@pytest.fixture
def recovery(git_history, tmp_path):
    base = git_history.tip
    git_history.update({"feature.txt": "done\n"})
    head = git_history.tip
    git_command(git_history.origin, "update-ref", "refs/heads/foragent/issue-1", head)
    git_command(git_history.origin, "update-ref", "refs/heads/main", base)
    git = GitRepo(git_history.clone)
    git.fetch()
    clock = [BASE_TIME + timedelta(hours=1)]
    tracker = FakeTracker(now=lambda: clock[0], strict=True)
    body = ("## Goal\nImplement the feature.\n## Out of scope\nOther features.\n## Done when\n"
            f"```sh\ntest -f feature.txt\n```\n```sh\ntest -f {ACCEPTANCE}\n```\n")
    tracker.add_issue(1, "Feature", body, labels=["blocked"])
    tracker.record_label(1, "approved", "human")
    pr = tracker.create_pr(head="foragent/issue-1", base="main", title="Keep title", body="Keep body")
    tracker.set_pr_head_sha(pr, head)
    tracker.set_pr_files(pr, ["feature.txt"])
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    store.save_approval(1, body=body, body_sha=body_hash(body),
                        acceptance_blobs=git.blob_shas("origin/main", [ACCEPTANCE]),
                        approved_by="human", approved_at=clock[0].isoformat())
    calls = []

    def executor_factory(name):
        assert name == "codex", "Recovery invoked an implementer"

        def run(**kwargs):
            calls.append(kwargs)
            return ExecResult(0, False, "VERDICT: PASS", kwargs["log_path"])

        return SimpleNamespace(run=run)

    ctx = RunContext("owner/repo", git_history.clone, tracker, store,
                     Config(review="fast", executor="claude", reviewer="codex"), git,
                     now=lambda: clock[0], executor_factory=executor_factory,
                     sleep=lambda seconds: clock.__setitem__(0, clock[0] + timedelta(seconds=seconds)),
                     monotonic=lambda: clock[0].timestamp())
    return SimpleNamespace(ctx=ctx, tracker=tracker, store=store, pr=pr, head=head, base=base,
                           history=git_history, calls=calls)


def assert_audited(case, kind):
    event = json.loads((case.store.root / "orch-events.jsonl").read_text().splitlines()[-1])
    assert event["kind"] == kind and event["issue"] == 1
    assert any(comment.body == f"<!-- foragent:orch -->\n{event['summary']}"
               for comment in case.tracker.list_comments(1))
    assert all(event.actor == "human" for event in case.tracker.label_events(1)
               if event.label == "approved" and event.action == "labeled")


def test_resolve_pass_can_merge_without_invoking_an_executor(recovery):
    c = recovery
    assert orch.resolve(c.ctx, 1, "pass", "Verified the disputed finding") == "merged"
    assert c.calls == []
    assert c.tracker.merged_prs == [c.pr]
    assert c.tracker.get_issue(1).state == "closed"
    assert c.store.get_run(1).outcome == "merged"
    assert not c.store.worktree_path(1).exists()
    assert c.tracker.created_prs[0].title == "Keep title"
    assert c.tracker.created_prs[0].body == "Keep body"
    assert_audited(c, "resolve")


@pytest.mark.parametrize("mode", ["careful", "batch"])
def test_resolve_retains_manual_review_policy(recovery, mode):
    c = recovery
    c.ctx.config = replace(c.ctx.config, review=mode)
    c.tracker.add_issue(10, "Parent")
    c.tracker.add_sub_issue(10, 1)
    assert orch.resolve(c.ctx, 1, "pass", "Reviewed") == "pr-open"
    assert c.tracker.merged_prs == []
    assert c.tracker.get_issue(c.pr).state == "open"


@pytest.mark.parametrize("approval", ["missing-snapshot", "removed-label", "edited-body"])
def test_resolve_cannot_supply_or_refresh_approval(recovery, approval):
    c = recovery
    if approval == "missing-snapshot":
        c.store.clear_approval(1)
    elif approval == "removed-label":
        c.tracker.remove_labels(1, ["approved"])
    else:
        c.tracker.update_issue(1, title="Feature", body=c.tracker.get_issue(1).body + "Changed scope")
    assert orch.resolve(c.ctx, 1, "pass", "Reviewed") in {"no-approval", "hash-mismatch"}
    assert c.tracker.merged_prs == [] and c.calls == []
    assert_audited(c, "resolve")


def test_changed_head_during_review_never_merges(recovery):
    c = recovery

    def factory(name):
        def run(**kwargs):
            c.tracker.set_pr_head_sha(c.pr, c.base)
            return ExecResult(0, False, "VERDICT: PASS", kwargs["log_path"])
        return SimpleNamespace(run=run)

    c.ctx.executor_factory = factory
    assert orch.rerun_review(c.ctx, 1) == "head-changed"
    assert c.tracker.merged_prs == []
    assert c.tracker.get_issue(c.pr).state == "open"
    assert_audited(c, "rerun-review")


def test_mutating_done_when_cannot_create_an_unreviewed_merge(recovery):
    c = recovery
    body = c.tracker.get_issue(1).body.replace("test -f feature.txt", "printf changed > feature.txt")
    snapshot = c.store.load_approval(1)
    c.tracker.update_issue(1, title="Feature", body=body)
    c.store.save_approval(1, body=body, body_sha=body_hash(body), acceptance_blobs=snapshot.acceptance_blobs,
                          approved_by="human", approved_at=snapshot.approved_at)
    assert orch.resolve(c.ctx, 1, "pass", "Reviewed") == "checks-modified-files"
    assert c.tracker.merged_prs == []
    assert c.tracker.pr_head_sha(c.pr) == c.head


def test_no_machine_checks_cannot_be_overridden(recovery):
    c = recovery
    body = "## Goal\nFeature\n## Done when\n- [ ] Looks good\n"
    c.tracker.update_issue(1, title="Feature", body=body)
    c.store.save_approval(1, body=body, body_sha=body_hash(body), acceptance_blobs={},
                          approved_by="human", approved_at=c.ctx.now().isoformat())
    assert orch.resolve(c.ctx, 1, "pass", "Reviewed") == "no-machine-check"
    assert c.tracker.merged_prs == []


def test_pending_ci_is_bounded_and_does_not_merge(recovery):
    c = recovery
    c.tracker.set_pr_checks(c.pr, "pending")
    assert orch.resolve(c.ctx, 1, "pass", "Reviewed") == "timeout"
    assert c.tracker.merged_prs == []
    assert c.store.get_run(1).status == "finished"
    assert "blocked" not in c.tracker.get_issue(1).labels


def test_raw_rerun_review_is_preserved_without_overwriting_prior_round(recovery):
    c = recovery
    prior = c.store.root / "runs/issue-1/review-8.txt"
    prior.parent.mkdir(parents=True)
    prior.write_text("Old review")
    assert orch.rerun_review(c.ctx, 1) == "merged"
    assert prior.read_text() == "Old review"
    assert prior.with_name("review-9.txt").read_text() == "VERDICT: PASS"


@pytest.mark.parametrize("recommend", [0, -1, 3])
def test_bad_recommendation_has_no_partial_escalation(recovery, recommend):
    c = recovery
    with pytest.raises(orch.OrchError, match="1-based"):
        orch.escalate(c.ctx, 1, "Choose", "Reviewed twice", ["A", "B"], recommend)
    assert "needs-human" not in c.tracker.get_issue(1).labels
    assert c.tracker.list_comments(1) == []


def test_active_runner_cannot_be_retried_or_resolved(recovery):
    c = recovery
    c.store.start_run(1, branch="foragent/issue-1", worktree=c.store.worktree_path(1))
    for action in (lambda: orch.retry(c.ctx, 1, "hint"), lambda: orch.resolve(c.ctx, 1, "fail", "note"),
                   lambda: orch.rerun_review(c.ctx, 1)):
        with pytest.raises(orch.OrchError, match="active runner"):
            action()
    assert c.tracker.get_issue(c.pr).state == "open"
    assert "blocked" in c.tracker.get_issue(1).labels
    assert c.tracker.list_comments(1) == []


def test_retry_without_hint_discards_previous_hint_and_only_exact_issue_pr(recovery):
    c = recovery
    other = c.tracker.create_pr(head="foragent/issue-10", base="main", title="Other", body="Other")
    hint = c.store.root / "hints/1.txt"
    hint.parent.mkdir(parents=True)
    hint.write_text("Old hint")
    orch.retry(c.ctx, 1)
    assert not hint.exists()
    assert c.tracker.get_issue(c.pr).state == "closed"
    assert c.tracker.get_issue(other).state == "open"
    assert_audited(c, "retry")


def test_list_uses_latest_github_blocker_and_exact_log_tail(recovery):
    c = recovery
    c.tracker.comment(1, "<!-- foragent:blocked reason=stalled -->\nOld details")
    c.tracker.comment(1, "<!-- foragent:blocked reason=new-reason -->\n새로운 상세")
    c.tracker.comment(1, "Unrelated newer comment")
    log = c.store.log_path(1)
    log.parent.mkdir(parents=True)
    log.write_text("".join(f"line {i}\n" for i in range(100)))
    [item] = orch.list_blocked(c.ctx)
    assert item["reason"] == "new-reason"
    assert item["details"] == "새로운 상세"
    assert item["log_tail"].splitlines() == [f"line {i}" for i in range(40, 100)]
    assert item["log_path"] == str(log)


@pytest.mark.parametrize("native_goal", [False, True])
def test_hint_preserves_native_goal_invocation(native_goal):
    parsed = parse_body("## Goal\nFeature\n## Done when\n```sh\ntrue\n```\n")
    prompt = build_prompt(1, parsed, "tests/acceptance/", native_goal=native_goal,
                          orchestrator_hint="Keep the selected locale")
    assert prompt.startswith("/goal " if native_goal else "Orchestrator hint:")
    assert "Orchestrator hint:\nKeep the selected locale\n\nYou are implementing" in prompt
