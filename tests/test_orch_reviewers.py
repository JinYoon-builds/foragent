"""Boundary tests for per-issue review panels (#533) beyond the locked acceptance file."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "acceptance"))

from foragent import orch_reviewers, review_perspectives  # noqa: E402
from foragent.config import Config  # noqa: E402
from foragent.executors import ExecResult  # noqa: E402
from foragent.runner import run_issue  # noqa: E402
from foragent.state import StateStore  # noqa: E402
from test_review_parallel import PanelExecutor, install_panel  # noqa: E402
from test_review_perspectives import perspective_case, review_offline  # noqa: E402,F401
from test_risk_map import copy_history, offline  # noqa: E402,F401
from test_skills_autosync import isolated_home  # noqa: E402,F401


def test_override_wins_rejects_below_one_and_clears(tmp_path):
    store = StateStore(tmp_path / "state")
    config = Config(review_reviewers=2)
    assert orch_reviewers.effective(store, config, 1) == 2
    orch_reviewers.set_count(store, 1, 4)
    try:
        orch_reviewers.set_count(store, 1, 0)
    except ValueError:
        pass
    else:
        raise AssertionError("count 0 must be rejected")
    assert orch_reviewers.effective(StateStore(tmp_path / "state"), config, 1) == 4
    assert orch_reviewers.effective(store, config, 2) == 2
    orch_reviewers.clear(store, 1)
    assert orch_reviewers.effective(store, config, 1) == 2


def test_shares_are_round_robin_and_capped():
    names = [item["name"] for item in review_perspectives.perspectives(Config())]
    assert [[item["name"] for item in share] for share in review_perspectives.shares(Config(), 2)] == [
        names[0::2], names[1::2]]
    assert len(review_perspectives.shares(Config(), 99)) == len(names)
    assert len(review_perspectives.shares(Config(), 1)) == 1


class LimitPanel(PanelExecutor):
    """Reviewer 1 hits a usage limit on its first call only; later calls behave normally."""

    def __init__(self, messages, **kwargs):
        super().__init__(messages, **kwargs)
        self.limited = False

    def run(self, **kwargs):
        result = super().run(**kwargs)
        with self.lock:
            hit = not self.limited and len(self.reviews) <= len(self.messages) and kwargs in self.reviews[1:2]
            self.limited |= hit
        if hit:
            return ExecResult(1, False, "usage limit reached", kwargs["log_path"], error_kind="limit")
        return result


def test_panel_usage_limit_is_limit_wait_and_discards_the_round(git_history, tmp_path, monkeypatch):
    case = perspective_case(git_history, tmp_path, monkeypatch, count=2, review_max=0, max_retries=1)
    panel = LimitPanel(["VERDICT: PASS"] * 2)
    install_panel(case, panel)
    outcome = run_issue(1, case.ctx)
    assert outcome == "limit-wait"
    assert len(panel.reviews) == 2 and panel.merges == [], "A limited round must not reach the merge call"
    labels = case.tracker.get_issue(1).labels
    assert "ready" in labels and "blocked" not in labels
    runs = case.store.root / "runs" / "issue-1"
    assert not list(runs.glob("review-*.txt")), "A discarded round must not count as a review round"


def test_parallel_reviewers_get_distinct_log_paths(git_history, tmp_path, monkeypatch):
    case = perspective_case(git_history, tmp_path, monkeypatch, count=3, review_max=0, max_retries=1)
    panel = PanelExecutor(["VERDICT: PASS"] * 3)
    install_panel(case, panel)
    assert run_issue(1, case.ctx) == "merged"
    paths = [call["log_path"] for call in panel.reviews]
    assert len(set(paths)) == 3, "Concurrent reviewers must not share a log (and its derived .stdout file)"
    assert case.store.log_path(1) not in paths


def test_reviewer_without_verdict_makes_round_unusable_without_merge(git_history, tmp_path, monkeypatch):
    case = perspective_case(git_history, tmp_path, monkeypatch, count=2, review_max=0, max_retries=1)
    panel = PanelExecutor(["VERDICT: PASS", "I could not finish the review."])
    install_panel(case, panel)
    outcome = run_issue(1, case.ctx)
    assert panel.merges == [], "A reviewer with no verdict must not reach the merge call"
    assert outcome != "merged" and case.tracker.merged_prs == []
    assert "blocked" in case.tracker.get_issue(1).labels


class OutcomePanel(PanelExecutor):
    """Reviewers overlap as usual, then reviewer k returns or raises ``outcomes[k]`` instead."""

    def __init__(self, messages, outcomes, *, merge_error=None):
        super().__init__(messages)
        self.outcomes = outcomes
        self.merge_error = merge_error
        self.started = 0

    def run(self, **kwargs):
        with self.lock:
            index, self.started = self.started, self.started + 1
        result = super().run(**kwargs)
        if index >= len(self.messages):
            if self.merge_error is not None:
                raise self.merge_error
            return result
        outcome = self.outcomes.get(index)
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome == "transient":
            return ExecResult(1, False, "service unavailable", kwargs["log_path"], error_kind="transient")
        if outcome == "limit":
            return ExecResult(1, False, "usage limit reached", kwargs["log_path"], error_kind="limit")
        return result


def run_outcome_panel(git_history, tmp_path, monkeypatch, panel):
    case = perspective_case(git_history, tmp_path, monkeypatch, count=len(panel.messages), review_max=0,
                            max_retries=1, transient_max_wait_minutes=0)
    install_panel(case, panel)
    return case, run_issue(1, case.ctx)


def test_usage_limit_wins_over_another_reviewers_exhausted_retries(git_history, tmp_path, monkeypatch):
    for order in ({0: "transient", 1: "limit"}, {0: "limit", 1: "transient"}):
        root = tmp_path / "-".join(order.values())
        panel = OutcomePanel(["VERDICT: PASS"] * 2, order)
        case, outcome = run_outcome_panel(copy_history(git_history, root), root, monkeypatch, panel)
        assert outcome == "limit-wait", order
        labels = case.tracker.get_issue(1).labels
        assert "ready" in labels and "blocked" not in labels, (order, labels)
        assert panel.merges == []


def test_reviewer_and_merge_errors_make_the_round_unusable(git_history, tmp_path, monkeypatch):
    cases = {
        "exhausted": OutcomePanel(["VERDICT: PASS"] * 2, {1: "transient"}),
        "raised": OutcomePanel(["VERDICT: PASS"] * 2, {0: RuntimeError("reviewer crashed")}),
        "merge-raised": OutcomePanel(["VERDICT: PASS"] * 2, {}, merge_error=RuntimeError("merge crashed")),
    }
    for name, panel in cases.items():
        root = tmp_path / name
        case, outcome = run_outcome_panel(copy_history(git_history, root), root, monkeypatch, panel)
        assert outcome != "merged" and case.tracker.merged_prs == [], name
        assert "blocked" in case.tracker.get_issue(1).labels, name
        merge_round = case.store.root / "runs" / "issue-1" / "review-1.txt"
        assert merge_round.is_file(), f"{name}: an unusable panel still counts as one review round"
        if name != "merge-raised":
            assert panel.merges == [], f"{name}: a failed reviewer must not reach the merge call"
