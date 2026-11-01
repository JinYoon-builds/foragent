"""Boundary tests for review spec conflicts (#673) beyond the locked acceptance file."""

import json
from pathlib import Path
import sys
from threading import Barrier, Event

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
    enable_native_reviewer(c, [json.dumps({"result": f"- feature.txt:1 — bug\n  class: stale-base\n{CONFLICT}"})])
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
    """Responses belong to log identities, independent of thread arrival order."""

    def __init__(self, *, first_arrival=1, first_completed=2, conflict=True):
        super().__init__(["VERDICT: PASS"] * len(CHECKLIST))
        self.first_arrival = first_arrival
        self.first_completed = first_completed
        self.conflict = conflict
        self.arrived = Event()
        self.both_running = Barrier(2)
        self.finished = {k: Event() for k in range(1, len(CHECKLIST) + 1)}
        self.responses = {}
        self.outcomes = {}
        self.arrivals = []
        self.completions = []

    @staticmethod
    def member(path):
        return int(path.stem.rsplit("-", 1)[1]) if "-review-" in path.stem else None

    def observe(self, monkeypatch):
        from foragent import run_review

        original = run_review._available_executor

        def execute(*args, **kwargs):
            k = self.member(kwargs["log_path"])
            if k is None:
                return original(*args, **kwargs)
            # Queued perspectives encounter the recorded pause, after both active members finish.
            if k > 2:
                assert self.finished[1].wait(5) and self.finished[2].wait(5)
            try:
                result = original(*args, **kwargs)
                self.outcomes[k] = "returned"
                return result
            except BaseException as error:
                self.outcomes[k] = getattr(error, "outcome", type(error).__name__)
                raise
            finally:
                with self.lock:
                    self.completions.append(k)
                self.finished[k].set()

        monkeypatch.setattr(run_review, "_available_executor", execute)

    def run(self, **kwargs):
        k = self.member(kwargs["log_path"])
        if k is None:
            self.merges.append(kwargs)
            return ExecResult(0, False, "VERDICT: PASS", kwargs["log_path"])
        if k in (1, 2):
            if k != self.first_arrival:
                assert self.arrived.wait(5)
            with self.lock:
                self.arrivals.append(k)
            if k == self.first_arrival:
                self.arrived.set()
            # Both have passed admission before either can record a limit.
            self.both_running.wait(timeout=5)
            if k != self.first_completed:
                assert self.finished[self.first_completed].wait(5)
        with self.lock:
            self.calls.append(kwargs)
            self.reviews.append(kwargs)
            self.responses[k] = "limit" if k == 2 else "conflict" if self.conflict and k == 1 else "pass"
        if k == 2:
            return ExecResult(1, False, "usage limit reached", kwargs["log_path"], error_kind="limit")
        message = f"VERDICT: FAIL\n{CONFLICT}" if self.conflict and k == 1 else "VERDICT: PASS"
        return ExecResult(0, False, message, kwargs["log_path"])


def install_ordered_panel(case, monkeypatch, **options):
    panel = ConflictThenLimitPanel(**options)
    install_panel(case, panel)
    panel.observe(monkeypatch)
    return panel


def assert_member_outcomes(panel):
    assert panel.responses == {1: "conflict" if panel.conflict else "pass", 2: "limit"}
    assert panel.arrivals == [panel.first_arrival, 3 - panel.first_arrival]
    assert panel.completions[:2] == [panel.first_completed, 3 - panel.first_completed]
    assert panel.outcomes == {1: "returned", 2: "limit-wait", 3: "limit-wait", 4: "limit-wait"}


def assert_conflict_result(case, panel):
    assert_member_outcomes(panel)
    assert len(questions(case.tracker)) == 1 and CONFLICT in questions(case.tracker)[0]
    assert [event["verdict"] for event in review_events(case.store)] == ["SPEC-CONFLICT"]
    assert panel.merges == []
    assert "ready" not in case.tracker.get_issue(1).labels
    assert "blocked" in case.tracker.get_issue(1).labels


def test_panel_member_conflict_wins_over_another_members_usage_limit(git_history, tmp_path, monkeypatch):
    case = perspective_case(git_history, tmp_path, monkeypatch, count=2, review_max=0, max_retries=1)
    panel = install_ordered_panel(case, monkeypatch)
    assert run_issue(1, case.ctx) == "spec-question"
    assert panel.merges == [], "A member conflict needs no merge call"
    assert CONFLICT in questions(case.tracker)[0]
    assert [event["verdict"] for event in review_events(case.store)] == ["SPEC-CONFLICT"]
    assert_conflict_result(case, panel)


@pytest.mark.parametrize("first_arrival,first_completed", [(1, 1), (2, 1), (2, 2)])
def test_panel_conflict_with_reversed_member_orders_and_queued_perspectives(
    git_history, tmp_path, monkeypatch, first_arrival, first_completed,
):
    case = perspective_case(git_history, tmp_path, monkeypatch, count=2, review_max=0, max_retries=1)
    panel = install_ordered_panel(case, monkeypatch, first_arrival=first_arrival, first_completed=first_completed)
    assert run_issue(1, case.ctx) == "spec-question"
    assert_conflict_result(case, panel)


def test_panel_without_conflict_records_quota_and_requeues_once(git_history, tmp_path, monkeypatch):
    from foragent import board

    case = perspective_case(git_history, tmp_path, monkeypatch, count=2, review_max=0, max_retries=1)
    panel = install_ordered_panel(case, monkeypatch, conflict=False)
    ready_labels, ready_board = [], []
    add_labels, sync_status = case.tracker.add_labels, board.sync_status

    def labels(n, values):
        if "ready" in values:
            ready_labels.append(n)
        return add_labels(n, values)

    def status(ctx, n, value, **kwargs):
        if value == "Ready":
            ready_board.append(n)
        return sync_status(ctx, n, value, **kwargs)

    monkeypatch.setattr(case.tracker, "add_labels", labels)
    monkeypatch.setattr(board, "sync_status", status)
    assert run_issue(1, case.ctx) == "limit-wait"
    assert_member_outcomes(panel)
    assert panel.merges == [] and questions(case.tracker) == []
    assert ready_labels == [1] and ready_board == [1]
    assert "ready" in case.tracker.get_issue(1).labels
    assert "running" not in case.tracker.get_issue(1).labels
    assert "blocked" not in case.tracker.get_issue(1).labels
    notes = json.loads((case.store.root / "usage-recheck.json").read_text())
    pauses = json.loads((case.store.root / "executor-pauses.json").read_text())
    assert notes and {name: note["until"] for name, note in notes.items()} == pauses
    assert "usage limit" in case.store.get_run(1).detail.lower() or "paused" in case.store.get_run(1).detail.lower()
