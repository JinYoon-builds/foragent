"""Confirmation keeps the original evidence across retries and review-only runs (#1170)."""

from dataclasses import replace
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest

from conftest import git_command
from foragent.executors import ExecResult
from foragent.issue import parse_body
from foragent.run_prompts import CONFIRM_PROMPT, REVIEW_PROMPT, build_confirm_prompt
from foragent.run_review import _journal_review, _review
from foragent.runner import review_only, run_issue
from runner_support import BODY, enable_reviewer, review_context, run_case  # noqa: F401


FIRST = (
    "VERDICT: FAIL\nsrc/one.py:8 — an old writer wins\n"
    "  class: stale-owner\n  evidence: delayed response\n  instance: src/two.py:12\n"
    "src/three.py:9 — rollback loses pending work\n  class: partial-rollback"
)


def save(store, name, raw):
    path = store.root / "runs/issue-1" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw)
    return path


def events(store):
    return [json.loads(line) for line in (store.root / "orch-events.jsonl").read_text().splitlines()]


@pytest.mark.parametrize("reviewer,scope", [("codex", "claude-local"), ("claude", "claude-local"),
                                           ("claude", "pr-body")])
def test_later_rounds_use_one_prompt_with_the_earliest_complete_review(review_context, monkeypatch, reviewer, scope):
    ctx = review_context
    ctx.config = replace(ctx.config, reviewer=reviewer, review_scope=scope, reviewer_model="review-model")
    # Numeric order, not lexicographic or modification order; member artifacts are not rounds.
    save(ctx.store, "review-10.txt", "VERDICT: FAIL\nsrc/new.py:1 — LATEST-ONLY")
    save(ctx.store, "review-1-1.txt", "VERDICT: FAIL\nsrc/raw.py:1 — RAW-MEMBER")
    first = save(ctx.store, "review-2.txt", FIRST)
    rebuttal = ctx.git.root / ".foragent/REBUTTAL.md"
    rebuttal.parent.mkdir(exist_ok=True)
    rebuttal.write_text("- stale-owner: repaired both entry points")
    monkeypatch.setattr("foragent.run_review._lane_decisions", lambda *args: "RECORDED-CHOICE")

    def no_panel(*args, **kwargs):
        pytest.fail("Confirmation must not start another perspective review")

    monkeypatch.setattr("foragent.run_review._risk_notes", no_panel)
    calls = []

    def execute(**kwargs):
        calls.append(kwargs)
        return ExecResult(0, False, "tail only", kwargs["log_path"], full_message="VERDICT: PASS\nNOTE: design")

    ctx.executor_factory = lambda name: SimpleNamespace(run=execute)
    for number in (11, 12):
        result = _review(1, ctx, ctx.git, parse_body(BODY), "origin/main", round_number=number, pr=42)
        assert result.verdict == "PASS" and result.review_notes == ("design",)
        assert (ctx.store.root / f"runs/issue-1/review-{number}.txt").read_text() == result.raw
    assert len(calls) == 2 and first.read_text() == FIRST
    for call in calls:
        prompt = call["prompt"]
        assert prompt.startswith(CONFIRM_PROMPT) and FIRST in prompt
        assert "LATEST-ONLY" not in prompt and "RAW-MEMBER" not in prompt
        assert "repaired both entry points" in prompt and "RECORDED-CHOICE" in prompt
        assert "diff --git a/feature.txt" in prompt and parse_body(BODY).goal in prompt
        assert "review_level" not in call and "review_pr" not in call
        if reviewer == "claude":
            assert call["model"] == (ctx.config.tiers["l"]["claude_model"] or "review-model")
        else:
            assert call["model"] == "review-model" and call["effort"] == ctx.config.tiers["l"]["codex_effort"]
    assert [event["round"] for event in events(ctx.store) if event["kind"] == "review"] == [11, 12]


@pytest.mark.parametrize("first", [None, "VERDICT: PASS", "VERDICT: FAIL", "unrecognized output",
                                   "VERDICT: FAIL\nNOTE: design"])
def test_no_first_round_findings_keeps_the_full_review_path(review_context, first):
    ctx = review_context
    ctx.config = replace(ctx.config, reviewer="codex")
    save(ctx.store, "review-1-1.txt", FIRST)
    if first is not None:
        save(ctx.store, "review-1.txt", first)
    save(ctx.store, "review-2.txt", FIRST)  # Do not substitute later findings for an empty first round.
    if first is None:
        (ctx.store.root / "runs/issue-1/review-2.txt").unlink()
    calls = []

    def execute(**kwargs):
        calls.append(kwargs)
        return ExecResult(0, False, "VERDICT: PASS", kwargs["log_path"])

    ctx.executor_factory = lambda name: SimpleNamespace(run=execute)
    assert _review(1, ctx, ctx.git, parse_body(BODY), "origin/main", round_number=3).verdict == "PASS"
    assert len(calls) == 1 and calls[0]["prompt"].startswith(REVIEW_PROMPT)
    assert FIRST not in calls[0]["prompt"]


@pytest.mark.parametrize("raw", [FIRST, "Reviewer 1:\n" + FIRST + "\nReviewer 2 failed"])
@pytest.mark.parametrize("conflict", [False, True])
def test_unusable_first_round_with_raw_findings_repeats_full_review(review_context, raw, conflict):
    ctx = review_context
    ctx.config = replace(ctx.config, reviewer="codex")
    save(ctx.store, "review-1.txt", raw)
    _journal_review(1, ctx, "SPEC-CONFLICT" if conflict else "UNUSABLE", "", 1,
                    review_verdict="UNUSABLE" if conflict else None)
    calls = []

    def execute(**kwargs):
        calls.append(kwargs)
        return ExecResult(0, False, "VERDICT: PASS", kwargs["log_path"])

    ctx.executor_factory = lambda name: SimpleNamespace(run=execute)
    assert _review(1, ctx, ctx.git, parse_body(BODY), "origin/main", round_number=2).verdict == "PASS"
    assert len(calls) == 1 and calls[0]["prompt"].startswith(REVIEW_PROMPT)


@pytest.mark.parametrize("exit_code,timed_out", [(1, False), (0, True), (0, False)])
def test_confirmation_preserves_failure_conflicts_and_repository_restore(review_context, exit_code, timed_out):
    ctx = review_context
    ctx.config = replace(ctx.config, reviewer="claude")
    save(ctx.store, "review-1.txt", FIRST)
    conflict = "SPEC-CONFLICT: tests/acceptance/test_feature.py:1 requires X; finding wants Y"
    raw = f"VERDICT: FAIL\nfeature.txt:1 — still broken\n{conflict}\nNOTE: design"
    before = ctx.git.review_state()

    def execute(**kwargs):
        (ctx.git.root / "feature.txt").write_text("reviewer edit")
        ctx.git.stage_changes()
        return ExecResult(exit_code, timed_out, raw, kwargs["log_path"])

    ctx.executor_factory = lambda name: SimpleNamespace(run=execute)
    result = _review(1, ctx, ctx.git, parse_body(BODY), "origin/main", round_number=2)
    assert result.verdict == ("UNUSABLE" if exit_code or timed_out else "FAIL")
    assert result.conflicts == (conflict,) and result.review_notes == ("design",)
    assert result.timed_out == timed_out and result.raw == raw
    assert ctx.git.review_state() == before
    assert (ctx.store.root / "runs/issue-1/review-2.txt").read_text() == raw
    assert [event["verdict"] for event in events(ctx.store) if event["kind"] == "review"] == ["SPEC-CONFLICT"]


def test_confirmation_usage_limit_keeps_notes_without_saving_a_completed_round(run_case):
    c = run_case
    c.executor.steps.append({})
    calls = enable_reviewer(c, [FIRST, ExecResult(1, False, "usage limit reached\nNOTE: design",
                                                 c.store.log_path(1), error_kind="limit")])
    assert run_issue(1, c.ctx) == "limit-wait"
    assert len(calls) == 2 and FIRST in calls[1]["prompt"]
    assert not (c.store.root / "runs/issue-1/review-2.txt").exists()
    assert any(event["kind"] == "review-note" and event["summary"] == "design" for event in events(c.store))


def test_review_only_uses_saved_classes_with_one_confirmer(run_case):
    c = run_case
    c.store.finish_run(1, outcome="held")
    c.now[0] += timedelta(seconds=1)
    c.tracker.remove_labels(1, ["running"])
    base = c.history.tip
    c.history.update({"feature.txt": "done"})
    head = c.history.tip
    pr = c.tracker.create_pr(head="foragent/issue-1", base="main", title="Existing PR", body="Review")
    c.tracker.set_pr_head_sha(pr, head)
    git_command(c.history.origin, "update-ref", "refs/heads/foragent/issue-1", head)
    git_command(c.history.origin, "update-ref", "refs/heads/main", base)
    save(c.store, "review-1.txt", FIRST)
    calls = enable_reviewer(c, ["VERDICT: PASS"])
    assert review_only(1, c.ctx) == "merged"
    assert len(calls) == 1 and FIRST in calls[0]["prompt"]
    assert calls[0]["prompt"].startswith(CONFIRM_PROMPT)
    assert c.executor.calls == [] and c.tracker.merged_prs == [pr]


def test_confirmation_keeps_every_first_round_evidence_line_when_diff_is_cut(tmp_path):
    first = FIRST + "\n" + "evidence\n" * 10000 + "LAST EVIDENCE"
    prompt = build_confirm_prompt(parse_body(BODY), "d" * 60001, first_review=first, full_text_dir=tmp_path)
    assert first in prompt and prompt.endswith("LAST EVIDENCE")
    assert "[cut: 1 more chars; full text:" in prompt
    assert (tmp_path / "review-diff.patch").read_text() == "d" * 60001
