from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from foragent.reasons import Reason, parse_reason
from foragent.runner import run_issue

from conftest import git_command
from runner_support import run_case, scripted_results


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


def test_question_stops_before_checks_and_is_truncated(run_case):
    c = run_case
    c.executor.steps = [{".foragent/QUESTION.md": "Choose A or B; recommend A.\n" + "q" * 7000}]
    assert run_issue(1, c.ctx) == "spec-question"
    comment = c.tracker.list_comments(1)[-1].body
    assert parse_reason(comment) == Reason.SPEC_QUESTION
    assert "Choose A or B; recommend A." in comment
    # The 6000-character limit stays and names what it left out (#420).
    details = comment.split("\n\n", 1)[1]
    kept, notice = details.split("[cut: ", 1)
    assert len(kept) == 6000 and kept.endswith("q" * 100)
    assert notice.startswith(f"{len('Choose A or B; recommend A.') + 1 + 7000 - 6000} more chars; full text: ")
    saved = Path(notice.split("full text: ", 1)[1].rstrip("]"))
    assert saved.read_text().endswith("q" * 7000)
    assert not any(args[:1] == ["add"] for _, args in c.operations)
    assert c.tracker.created_prs == []


def test_remaining_budget_model_and_progress_are_passed_through(run_case):
    c = run_case
    c.ctx.config = replace(c.ctx.config, executor_model="chosen")
    c.now[0] += timedelta(minutes=2)
    assert run_issue(1, c.ctx) == "merged"
    assert c.executor.calls[0]["timeout_s"] == (90 - 2) * 60
    assert c.executor.calls[0]["model"] == "chosen"
    assert c.store.get_run(1).last_progress_at == c.now[0].isoformat()


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
