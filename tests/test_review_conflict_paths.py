"""Boundary tests for review spec conflicts (#673) beyond the locked acceptance file."""

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "acceptance"))

from conftest import ACCEPTANCE  # noqa: E402
from foragent.executors import ExecResult  # noqa: E402
from foragent.run_review import spec_conflicts  # noqa: E402
from foragent.runner import run_issue  # noqa: E402
from runner_support import enable_native_reviewer, run_case  # noqa: E402,F401
from test_review_parallel import PanelExecutor, install_panel  # noqa: E402
from test_review_perspectives import CHECKLIST, perspective_case, review_offline  # noqa: E402,F401
from test_risk_map import copy_history, offline  # noqa: E402,F401
from test_skills_autosync import isolated_home  # noqa: E402,F401

CONFLICT = f"SPEC-CONFLICT: {ACCEPTANCE}:1 requires a closed talk; the finding wants it open"
QUESTION = "<!-- foragent:blocked reason=spec-question -->"


def review_events(store):
    path = store.root / "orch-events.jsonl"
    return [event for event in map(json.loads, path.read_text().splitlines()) if event["kind"] == "review"]


def questions(tracker):
    return [comment.body for comment in tracker.list_comments(1) if comment.body.startswith(QUESTION)]


def test_spec_conflicts_keeps_every_distinct_prefixed_line_in_order():
    text = f"VERDICT: FAIL\n  {CONFLICT}\nfeature.txt:1 bug\nnote SPEC-CONFLICT: inline\nSPEC-CONFLICT: b"
    assert spec_conflicts(text, f"SPEC-CONFLICT: b\n{CONFLICT}") == (CONFLICT, "SPEC-CONFLICT: b")
    assert spec_conflicts("VERDICT: PASS") == ()


def test_code_review_conflict_outside_json_stops_whatever_the_verdict(run_case):
    c = run_case
    calls = enable_native_reviewer(c, [json.dumps({"result": f"[]\n{CONFLICT}"})])
    assert run_issue(1, c.ctx) == "spec-question"
    assert len(calls) == len(c.executor.calls) == 1
    assert len(questions(c.tracker)) == 1 and CONFLICT in questions(c.tracker)[0]
    assert "needs-human" not in c.tracker.get_issue(1).labels
    assert [event["verdict"] for event in review_events(c.store)] == ["SPEC-CONFLICT"]
    assert c.tracker.merged_prs == []


def test_conflict_round_keeps_the_reviewers_own_verdict_and_classes_beside_it(run_case):
    from foragent.orch_facts_reviews import _artifacts

    c = run_case
    enable_native_reviewer(c, [json.dumps({"result": f"VERDICT: FAIL\n- feature.txt:1 — bug\n  class: stale-base\n{CONFLICT}"})])
    assert run_issue(1, c.ctx) == "spec-question"
    event, = review_events(c.store)
    assert (event["verdict"], event["review_verdict"]) == ("SPEC-CONFLICT", "FAIL")
    assert [classes for _, _, classes in _artifacts(c.store)[1]] == [{"stale-base"}]


def test_panel_member_conflict_stops_without_a_merge_call_that_could_drop_it(git_history, tmp_path, monkeypatch):
    case = perspective_case(git_history, tmp_path, monkeypatch, count=2, review_max=0, max_retries=1)
    panel = PanelExecutor(["VERDICT: PASS", f"VERDICT: FAIL\n{CONFLICT}"] + ["VERDICT: PASS"] * (len(CHECKLIST) - 2),
                          merged="VERDICT: PASS")
    install_panel(case, panel)
    assert run_issue(1, case.ctx) == "spec-question"
    assert panel.merges == [] and len(case.implementer.calls) == 1
    assert CONFLICT in questions(case.tracker)[0]
    assert "blocked" in case.tracker.get_issue(1).labels
    assert case.tracker.merged_prs == []


def _previous_rules(acceptance_dir, question):
    return (
        "Block only on: the Goal is not met, something listed in Out of scope was changed, or a check was weakened.\n"
        f"Tests under {acceptance_dir} that belong to other issues fail on purpose until those issues land; "
        "do not report them.\nImprovements beyond this issue's Goal are not defects."
        + (f"\n{question}" if question else "")
    )


def test_older_pr_scope_gains_the_spec_conflict_rule_once():
    from foragent.i18n import text
    from foragent.run_prompts import review_scope_rules
    from foragent.run_review import with_review_scope

    rules = review_scope_rules("tests/acceptance/")
    for question in (text("review_class_question", "en"), ""):
        older = "Closes #1\n\n## Review scope\n\n" + _previous_rules("tests/acceptance/", question) + "\nTail."
        refreshed = with_review_scope(older, "tests/acceptance/", "en")
        assert rules in refreshed and refreshed.count("SPEC-CONFLICT:") == 1, refreshed
        assert refreshed.endswith("\nTail.")
        assert with_review_scope(refreshed, "tests/acceptance/", "en") == refreshed
    custom = with_review_scope("Closes #1\n\n## Review scope\n\nOnly the API.\n", "tests/acceptance/", "en")
    assert "SPEC-CONFLICT:" in custom and "locked acceptance test" in custom and "Only the API." in custom


class ConflictThenLimitPanel(PanelExecutor):
    """Reviewer 1 reports a spec conflict; reviewer 2 hits a usage limit."""

    def run(self, **kwargs):
        result = super().run(**kwargs)
        if kwargs in self.reviews[1:2]:
            return ExecResult(1, False, "usage limit reached", kwargs["log_path"], error_kind="limit")
        return result


@pytest.mark.parametrize("deferred_comments", [False, True], ids=["ordinary", "queued-comment"])
def test_panel_member_conflict_wins_over_another_members_usage_limit(
    git_history, tmp_path, monkeypatch, deferred_comments,
):
    if deferred_comments:
        from foragent.tracker import RecordingTracker

        def queue_comment(self, number, body):
            entry = self._queue(number, "comment", self._started(), self._premise(number), {"body": body})
            return -entry["id"]

        monkeypatch.setattr(RecordingTracker, "comment", queue_comment)
    case = perspective_case(git_history, tmp_path, monkeypatch, count=2, review_max=0, max_retries=1)
    panel = ConflictThenLimitPanel([f"VERDICT: FAIL\n{CONFLICT}"] + ["VERDICT: PASS"] * (len(CHECKLIST) - 1))
    install_panel(case, panel)
    assert run_issue(1, case.ctx) == "spec-question"
    assert panel.merges == [], "A member conflict needs no merge call"
    assert case.store.load_flag(1).reason == "spec-question"
    # Sibling limit-wait label writes may queue this comment behind an in-flight write.
    pending = case.store.github_outbox()
    if deferred_comments:
        assert not questions(case.tracker)
        assert any(entry["action"] == "comment" and CONFLICT in entry.get("body", "") for entry in pending), pending
    case.ctx.tracker.flush_outbox()
    comments = questions(case.tracker)
    assert comments, f"Spec-conflict question comment was not delivered; queued writes: {pending}"
    assert CONFLICT in comments[0]
    assert [event["verdict"] for event in review_events(case.store)] == ["SPEC-CONFLICT"]


def test_break_it_conflict_receipt_must_keep_the_reviewers_verdict(run_case, monkeypatch):
    from foragent import run_review

    journal = run_review._journal_review

    def lose_reviewer_verdict(*args, **kwargs):
        if "review_verdict" in kwargs:
            kwargs["review_verdict"] = "UNUSABLE"
        return journal(*args, **kwargs)

    monkeypatch.setattr(run_review, "_journal_review", lose_reviewer_verdict)
    with pytest.raises(AssertionError, match="FAIL"):
        test_conflict_round_keeps_the_reviewers_own_verdict_and_classes_beside_it(run_case)


@pytest.mark.parametrize("skip", ["flush", "send"])
def test_break_it_conflict_priority_requires_delivery_of_the_question(git_history, tmp_path, monkeypatch, skip):
    from foragent.tracker import FakeTracker, RecordingTracker

    # Keep the local flag and outcome, but skip either the flush or the external send.
    if skip == "flush":
        monkeypatch.setattr(RecordingTracker, "flush_outbox", lambda self: None)
    else:
        monkeypatch.setattr(FakeTracker, "comment", lambda *args, **kwargs: 1)
    with pytest.raises(AssertionError, match="question comment was not delivered"):
        test_panel_member_conflict_wins_over_another_members_usage_limit(git_history, tmp_path, monkeypatch, True)
