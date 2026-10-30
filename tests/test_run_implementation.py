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
    # #238: the turn again after the limit wait is a new turn with its own key.
    assert c.store.get_run(1).attempt_tiers == {"1": "m", "2": "l", "3": "l"}


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


def test_a_tier_without_effort_does_not_keep_the_previous_turns_effort(run_case):
    # #238: effort is chosen per turn; None means the executor default, not the last turn's value.
    c = run_case
    c.tracker.add_labels(1, ["size:m"])
    tiers = {**c.ctx.config.tiers, "l": {**c.ctx.config.tiers["l"], "codex_effort": None}}
    c.ctx.config = replace(c.ctx.config, tiers=tiers)
    c.executor.steps = [{"feature.txt": "wrong"}, {"feature.txt": "done"}]
    assert run_issue(1, c.ctx) == "merged"
    assert [call.get("effort") for call in c.executor.calls] == ["high", None]
    assert "effort" not in c.executor.calls[1]
    assert c.store.get_run(1).attempt_tiers == {"1": "m", "2": "l"}


@pytest.mark.parametrize("lane_model,expected", [(None, ["small-model", "medium-model"]),
                                                    ("lane-model", ["lane-model"] * 2)])
def test_claude_tier_model_and_lane_precedence(run_case, lane_model, expected):
    c = run_case
    c.tracker.add_labels(1, ["size:s", "lane:api"])
    c.store.update_run(1, lane="api")
    tiers = {"s": {"codex_effort": "medium", "claude_model": "small-model"},
             "m": {"codex_effort": "high", "claude_model": "medium-model"},
             "l": {"codex_effort": "xhigh", "claude_model": "large-model"}}
    # Two retries: the second attempt is not the last, so it steps up one tier only (#238).
    c.ctx.config = replace(c.ctx.config, executor="codex", executor_model="global-model", tiers=tiers,
                           lanes={"api": {"executor": "claude", "model": lane_model}}, max_retries=2)
    kinds = []
    c.ctx.executor_factory = lambda name: kinds.append(name) or c.executor
    c.executor.steps = [{"feature.txt": "wrong"}, {"feature.txt": "done"}]
    assert run_issue(1, c.ctx) == "merged"
    assert kinds == ["claude"]
    assert [call["model"] for call in c.executor.calls] == expected
    assert all("effort" not in call for call in c.executor.calls)


def test_new_attempt_reads_the_saved_failure_bundle_not_the_short_detail(run_case):
    """#420: a retry after rerun-review gets every saved failing command, whatever the run detail says."""
    from foragent import cuts

    c = run_case
    bundle = "Command: first\nfirst output\n\nCommand: second\nsecond output"
    cuts.save_full(cuts.failures_path(c.store.root, 1).parent, cuts.FAILURES_FILE, bundle)
    c.store.update_run(1, detail="Done when failed on the PR head.", resume_reason="checks-failed")
    assert run_issue(1, c.ctx) == "merged"
    prompt = c.executor.calls[0]["prompt"]
    assert "first output" in prompt and "second output" in prompt
    assert prompt.count("second output") == 1


@pytest.mark.parametrize("resumed", [False, True], ids=["fresh", "resumed"])
def test_saved_review_reaches_the_next_attempt_whole(run_case, resumed):
    """#420: after rerun-review FAIL then retry, every saved finding reaches the implementer, fresh or resumed."""
    from foragent import cuts

    c = run_case
    findings = [f"src/feature_{n}.py:{n} — retry drops required field_{n}. " + "x" * 400 for n in range(1, 10)]
    review = cuts.issue_dir(c.store.root, 1) / "review-1.txt"
    review.parent.mkdir(parents=True, exist_ok=True)
    review.write_text("VERDICT: FAIL\n" + "\n".join(findings), encoding="utf-8")
    c.store.update_run(1, detail="Re-run review FAIL.", resume_reason="retries-exceeded\nRe-run review FAIL.")
    if resumed:
        c.store.update_run(1, session_id="saved", session_executor=c.ctx.config.executor,
                           session_body=c.store.load_approval(1).body)
    assert run_issue(1, c.ctx) == "merged"
    call = c.executor.calls[0]
    assert (call.get("resume_session_id") == "saved") is resumed
    missing = [finding for finding in findings if finding not in call["prompt"]]
    assert not missing, f"{len(missing)} of 9 saved findings lost"


def test_exception_during_checks_keeps_the_failures_gathered_before_it(run_case, monkeypatch):
    """#420: command A fails, launching command B raises; A's failure still reaches the next attempt."""
    from foragent import cuts, runner
    from foragent.run_checks import CheckResult
    from runner_support import set_done_when

    c = run_case
    set_done_when(c, ["check-a", "check-b"])

    def check(command, *args, **kwargs):
        if command == "check-b":
            raise OSError("cannot launch check-b")
        return CheckResult(command, 1, False, "A failed output")

    monkeypatch.setattr(runner, "_check", check)
    run_issue(1, c.ctx)
    assert "A failed output" in cuts.failures_path(c.store.root, 1).read_text()


def test_duplicate_done_when_command_failure_is_not_hidden_by_a_later_pass(run_case, monkeypatch):
    """#420: the same command listed twice, failing once, still fails verification."""
    from foragent import runner
    from foragent.run_checks import CheckResult
    from runner_support import set_done_when

    c = run_case
    set_done_when(c, ["check-same", "check-same"])
    c.ctx.config = replace(c.ctx.config, max_retries=0)
    runs = []

    def check(command, *args, **kwargs):
        runs.append(command)
        return CheckResult(command, 1 if len(runs) == 1 else 0, False, "first invocation broke")

    monkeypatch.setattr(runner, "_check", check)
    assert run_issue(1, c.ctx) != "merged"


def test_reverification_interruption_keeps_first_pass_failures(run_case, monkeypatch):
    """#420: checks change tracked files, the second pass cannot launch; first-pass failures survive."""
    from foragent import cuts, runner
    from foragent.run_checks import CheckResult
    from runner_support import set_done_when

    c = run_case
    set_done_when(c, ["check-a"])
    passes = []

    def check(command, worktree, *args, **kwargs):
        passes.append(command)
        if len(passes) == 1:
            (Path(worktree) / f"generated-{len(passes)}.txt").write_text("changed by the check")
            return CheckResult(command, 1, False, "first pass broke")
        raise OSError("second pass cannot launch")

    monkeypatch.setattr(runner, "_check", check)
    run_issue(1, c.ctx)
    assert "first pass broke" in cuts.failures_path(c.store.root, 1).read_text()
