"""Acceptance test for whole-picture. Locked by Foragent; do not edit."""

from datetime import timedelta
from pathlib import Path
import re
import socket
from types import SimpleNamespace

import pytest

from test_orch_review_handoff import make_case, tick_context


# Shared cut notice: [cut: N more chars|lines; full text: <path>].
# Removing a limit and delivering the full text also satisfies the contract.
CUT_NOTICE_MARKER = "[cut:"
CUT_NOTICE = re.compile(
    r"\[cut: (?P<count>[1-9]\d*) more (?P<unit>chars|lines); full text: (?P<path>[^\]\r\n]+)\]"
)


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    from foragent.executors import ClaudeExecutor, CodexExecutor

    def forbidden(*args, **kwargs):
        pytest.fail("Whole-picture acceptance tests must not use a real model or network")

    monkeypatch.setattr(ClaudeExecutor, "run", forbidden)
    monkeypatch.setattr(CodexExecutor, "run", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


def prepare_attempt(case, n=1):
    """Use a real temporary worktree, keeping prompt artifacts available to read."""
    from foragent.run_implementation import _prepare_implementation

    case.git.fetch("origin", case.config.base_branch)
    worktree = case.store.worktree_path(n)
    case.git.add_worktree(worktree, f"foragent/issue-{n}", f"origin/{case.config.base_branch}")
    return _prepare_implementation(n, case.ctx, case.git.at(worktree), target_branch=case.config.base_branch)


def assert_whole_or_explicit_cut(delivery):
    """Validate delivered text, exact omitted quantity, and a readable full copy."""
    prompt, full = delivery.prompt, delivery.full_text
    if full in prompt:
        return
    notice = CUT_NOTICE.search(prompt)
    assert notice is not None, (
        f"{delivery.name} silently loses context: deliver it whole or use the shared "
        f"{CUT_NOTICE_MARKER} N more {delivery.unit}; full text: <path>] notice"
    )
    assert notice["unit"] == delivery.unit, f"{delivery.name}: wrong omitted-count unit"

    # Measure the prefix actually delivered, allowing an implementation to change
    # either limit or to omit the inline text entirely and link to the full copy.
    start = prompt.find(full.splitlines(keepends=True)[0])
    visible = 0
    if start >= 0:
        for expected, actual in zip(full, prompt[start:]):
            if expected != actual:
                break
            visible += 1
    rest = full[visible:]
    omitted = len(rest) if delivery.unit == "chars" else len(rest.splitlines())
    assert int(notice["count"]) == omitted, (
        f"{delivery.name}: notice reports {notice['count']} omitted {delivery.unit}, actually {omitted}"
    )
    path = Path(notice["path"].strip().strip("`"))
    if not path.is_absolute():
        path = delivery.worktree / path
    assert path.is_file(), f"{delivery.name}: full-text file does not exist: {path}"
    assert path.read_text(encoding="utf-8") == full, f"{delivery.name}: linked file must contain the full text"


@pytest.fixture
def reviewer_diff_delivery(git_history, tmp_path, monkeypatch):
    from foragent.run_implementation import _implement
    from foragent.run_review import _review

    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[])
    content = "".join(f"record-{n:04d}: preserve the complete review context and all constraints\n"
                      for n in range(1400))

    def implement(kwargs):
        (kwargs["worktree"] / "feature.txt").write_text(content, encoding="utf-8")
        return "Implemented"

    case.implementer.steps = [implement]
    attempt = prepare_attempt(case)
    _implement(attempt, "")
    attempt.git.stage_changes()
    full_diff = attempt.git.staged_diff(attempt.base_ref)
    assert len(full_diff) > 60000, "Fixture must exceed the review diff limit"
    review = _review(1, case.ctx, attempt.git, attempt.parsed, attempt.base_ref)
    assert review.verdict == "PASS"
    assert len(case.reviewer.calls) == 1
    return SimpleNamespace(name="Reviewer diff", prompt=case.reviewer.calls[0]["prompt"],
                           full_text=full_diff, unit="chars", worktree=attempt.worktree)


@pytest.fixture
def lane_memory_delivery(git_history, tmp_path, monkeypatch):
    from foragent.run_implementation import _implement

    memory = "".join(f"memory-{n:03d}: shared constraint {n:03d} applies to every issue.\n"
                     for n in range(1, 301))
    git_history.update({
        ".claude/agents/fa-picture.md": "---\nname: fa-picture\nmemory: project\n---\nPreserve shared context.\n",
        ".claude/agent-memory/fa-picture/MEMORY.md": memory,
    })
    case = make_case(git_history, tmp_path, monkeypatch, executor="codex", reviewer="claude",
                     skill_libraries=[], lanes={"picture": {"agent": "fa-picture", "skills": []}})
    case.store.update_run(1, lane="picture")
    attempt = prepare_attempt(case)
    _implement(attempt, "")
    assert len(case.implementer.calls) == 1
    return SimpleNamespace(name="Lane memory", prompt=case.implementer.calls[0]["prompt"],
                           full_text=memory, unit="lines", worktree=attempt.worktree)


def test_all_nine_review_findings_reach_next_implementer(git_history, tmp_path, monkeypatch):
    from foragent.runner import run_issue

    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[])
    findings = [f"src/feature_{n}.py:{n} — retry drops required field_{n}." for n in range(1, 10)]
    case.reviewer.steps = ["VERDICT: FAIL\n" + "\n".join(findings), "VERDICT: PASS"]
    case.implementer.steps = [case.implement, case.implement]
    assert run_issue(1, case.ctx) == "merged"
    assert len(case.implementer.calls) == 2
    prompt = case.implementer.calls[1]["prompt"]
    missing = [finding for finding in findings if finding not in prompt]
    assert not missing, f"Next implementer lost {len(missing)} of 9 review findings: {missing}"


def test_all_three_done_when_failures_reach_next_implementer(git_history, tmp_path, monkeypatch):
    from foragent import runner
    from foragent.issue import body_hash
    from foragent.run_checks import CheckResult

    case = make_case(git_history, tmp_path, monkeypatch, reviewer="none", skill_libraries=[])
    commands = ("python -m checks.schema", "python -m checks.routing", "python -m checks.storage")
    tails = dict(zip(commands, ("SCHEMA: required field is missing", "ROUTING: route drops the locale",
                               "STORAGE: transaction loses the receipt")))
    approval = case.store.load_approval(1)
    body = approval.body.partition("## Done when")[0] + "## Done when\n" + "".join(
        f"```sh\n{command}\n```\n" for command in commands
    )
    case.tracker.edit_body(1, body)
    case.store.save_approval(1, body=body, body_sha=body_hash(body), acceptance_blobs=approval.acceptance_blobs,
                             approved_by=approval.approved_by, approved_at=approval.approved_at)
    seen = []

    def check(command, worktree, log, timeout_s, **kwargs):
        assert command in commands, f"Unexpected check: {command}"
        seen.append(command)
        failed = len(seen) <= len(commands)
        return CheckResult(command, 1 if failed else 0, False, tails[command] if failed else "passed")

    monkeypatch.setattr(runner, "_check", check)
    case.implementer.steps = [case.implement, case.implement]
    assert runner.run_issue(1, case.ctx) == "merged"
    assert seen == list(commands) * 2
    assert len(case.implementer.calls) == 2
    prompt = case.implementer.calls[1]["prompt"]
    missing = [command for command in commands if command not in prompt or tails[command] not in prompt]
    assert not missing, f"Next implementer lost Done-when commands or output tails: {missing}"


def test_large_reviewer_diff_is_whole_or_links_to_full_diff(reviewer_diff_delivery):
    assert_whole_or_explicit_cut(reviewer_diff_delivery)


def test_non_batch_child_receives_published_parent_outcome_and_constraints(git_history, tmp_path, monkeypatch):
    from foragent import order, scheduler
    from foragent.runner import run_issue

    case = make_case(git_history, tmp_path, monkeypatch, review="fast", review_paths={},
                     test_command="true", skill_libraries=[])
    outcome = "Shared outcome: a receipt created by the API is readable by the client."
    constraints = ("All issues must use receipt_schema_v3 at the API/client boundary.",
                   "Preserve the original locale through storage and rendering.")
    summary = outcome + "\n\nCross-issue constraints:\n" + "\n".join(constraints)
    plan = {
        "id": "whole-picture", "title": "Ship receipts", "summary": summary, "spec_markdown": summary,
        "issues": [
            {"key": key, "title": title, "goal": goal, "out_of_scope": "Payments.",
             "done_when": ["test -f feature.txt"], "lane": "main", "size": "s"}
            for key, title, goal in (("api", "Create receipts", "Implement receipt creation."),
                                     ("client", "Read receipts", "Implement receipt rendering."))
        ],
    }
    published = order.publish(plan, case.root, case.config, case.tracker, repo=case.ctx.repo, git=case.git)
    child = published.rows[0]["number"]
    assert published.parent is not None and case.tracker.parent_of(child) == published.parent
    assert "foragent:plan" in case.tracker.get_issue(published.parent).body
    assert all(text not in case.tracker.get_issue(child).body for text in (outcome, *constraints))

    # Approve after publication and let the real offline scheduler freeze the
    # child's snapshot; no human, model, runner launch, or preread is needed here.
    case.tracker.remove_labels(1, ["approved", "running"])
    case.store.finish_run(1, outcome="merged")
    case.clock[0] += timedelta(seconds=1)
    case.tracker.record_label(child, "approved", "human")
    monkeypatch.setattr(scheduler, "machine_load", lambda: ("normal", 0.0))
    report = scheduler.tick(tick_context(case, start_runs=False))
    assert case.store.load_approval(child) is not None, report.actions
    case.store.start_run(child, branch=f"foragent/issue-{child}", worktree=case.store.worktree_path(child))
    assert run_issue(child, case.ctx) == "merged"
    assert len(case.implementer.calls) == 1
    prompt = case.implementer.calls[0]["prompt"]
    missing = [text for text in (outcome, *constraints) if text not in prompt]
    assert not missing, f"Non-batch child lost published parent outcome or cross-issue constraints: {missing}"


def test_300_line_lane_memory_is_whole_or_explicitly_cut(lane_memory_delivery):
    assert_whole_or_explicit_cut(lane_memory_delivery)


@pytest.mark.parametrize("source", ["reviewer_diff_delivery", "lane_memory_delivery"],
                         ids=["reviewer-diff", "lane-memory"])
def test_remaining_cuts_use_shared_cut_notice_marker(source, request):
    assert_whole_or_explicit_cut(request.getfixturevalue(source))
