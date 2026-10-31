"""Acceptance test for review-parallel. Locked by Foragent; do not edit."""

from dataclasses import asdict
from threading import Barrier, BrokenBarrierError, Lock
from types import SimpleNamespace

from foragent.executors import ExecResult
from foragent.issue import parse_body
from foragent.runner import run_issue
from foragent.state import StateStore
from runner_support import ScriptedExecutor
from test_orch_review_handoff import bind_cli
from test_risk_map import cli_ok, copy_history, offline
from test_review_perspectives import CHECKLIST, assert_checklist, perspective_case, review_offline
from test_skills_autosync import cli_exit, isolated_home


class PanelExecutor:
    """Observe real executor overlap; never implement scheduling or merging.

    The first N calls are independent reviews, the next call is the merger.
    No prompt keywords or unspecified merger role/option names are required.
    A broken barrier returns a normal fake result so the test can report the
    missing overlap at the test boundary, even if the runner catches exceptions.
    """

    def __init__(self, messages, merged="VERDICT: PASS", *, failed=None, timed_out=False, overlap=1):
        self.messages = messages
        self.merged = merged
        self.failed = failed
        self.timed_out = timed_out
        self.barrier = Barrier(min(overlap, len(messages)))
        self.active = self.maximum = 0
        self.lock = Lock()
        self.calls = []
        self.reviews = []
        self.merges = []
        self.completed = set()
        self.overlap_errors = []
        self.early_merge = False

    def run(self, **kwargs):
        with self.lock:
            index = len(self.calls)
            self.calls.append(kwargs)
            if index < len(self.messages):
                self.reviews.append(kwargs)
                self.active += 1
                self.maximum = max(self.maximum, self.active)
            else:
                self.merges.append(kwargs)
                self.early_merge |= len(self.completed) != len(self.messages)
        if index >= len(self.messages):
            return ExecResult(0, False, self.merged, kwargs["log_path"])
        if index < len(self.messages) - len(self.messages) % self.barrier.parties:
            try:
                self.barrier.wait(timeout=5.0)
            except BrokenBarrierError:
                with self.lock:
                    self.overlap_errors.append(index)
        with self.lock:
            self.active -= 1
            self.completed.add(index)
        failed = index == self.failed
        # Timeout is an executor-result flag, with no process or signal path.
        # Keep exit_code=0 in that case to exercise timeout handling itself.
        return ExecResult(1 if failed and not self.timed_out else 0, failed and self.timed_out,
                          self.messages[index], kwargs["log_path"])


def install_panel(case, panel, *, implement=True):
    def execute(**kwargs):
        if implement and not case.implementer.calls:
            return case.implementer.run(**kwargs)
        return panel.run(**kwargs)

    # The contract does not choose a model for the merge step. Capture calls to
    # either executor after the one ordinary implementation call.
    executor = SimpleNamespace(run=execute)
    case.ctx.executor_factory = lambda name: executor


def assert_panel(panel, count):
    assert len(panel.reviews) == count, f"Expected {count} independent reviewers, got {len(panel.reviews)}"
    assert not panel.overlap_errors, "Reviewers did not use the available concurrency before the next batch"
    assert len(panel.merges) == 1, f"Expected exactly one findings merge call, got {len(panel.merges)}"
    assert len(panel.calls) == count + 1
    assert not panel.early_merge, "Findings merge started before all reviewers returned"


def test_review_parallel_round_robin_reviews_overlap_and_merge_alone_decides(
    git_history, tmp_path, monkeypatch,
):
    case = perspective_case(git_history, tmp_path, monkeypatch, count=3, review_max=0, max_retries=1)
    messages = [f"VERDICT: FAIL\nfeature.txt:{index + 1} — RAW-CLAIM-{index}" for index in range(len(CHECKLIST))]
    # The merger dismisses these claims. A union/majority of reviewer verdicts
    # would block; only the one merge result is authoritative.
    panel = PanelExecutor(messages, merged="VERDICT: PASS", overlap=3)
    install_panel(case, panel)
    outcome = run_issue(1, case.ctx)
    assert_panel(panel, len(CHECKLIST))
    assert panel.maximum == 3
    expected = [[item] for item in CHECKLIST]
    shares = []
    for call in panel.reviews:
        prompt = call["prompt"]
        share = [item for item in CHECKLIST if item["focus"] in prompt]
        assert_checklist(prompt, share)
        shares.append(tuple(item["name"] for item in share))
    assert sorted(shares) == sorted(tuple(item["name"] for item in share) for share in expected)
    for index in range(len(CHECKLIST)):
        assert f"RAW-CLAIM-{index}" in panel.merges[0]["prompt"], "Merger lost a reviewer's findings"
    assert outcome == "merged" and case.tracker.merged_prs, "Only merge-call blockers may block the PR"


def test_review_parallel_deduped_merge_findings_reach_blocked_comment_once(
    git_history, tmp_path, monkeypatch,
):
    case = perspective_case(git_history, tmp_path, monkeypatch, count=2, review_max=0, max_retries=1)
    shared = "feature.txt:1 — SHARED-DEFECT: caller provenance is lost"
    left = "feature.txt:2 — LEFT-DEFECT: stale state can overwrite the receipt"
    right = "feature.txt:3 — RIGHT-DEFECT: partial responses lose the locale"
    dismissed = "feature.txt:4 — DISMISSED-CLAIM: outside the approved goal"
    panel = PanelExecutor([
        "VERDICT: FAIL\n" + "\n".join((shared, left, dismissed)),
        "VERDICT: FAIL\n" + "\n".join((shared, right)),
    ] + ["VERDICT: PASS"] * (len(CHECKLIST) - 2), merged="VERDICT: FAIL\n" + "\n".join((shared, left, right)))
    install_panel(case, panel)
    outcome = run_issue(1, case.ctx)
    assert_panel(panel, len(CHECKLIST))
    merge_prompt = panel.merges[0]["prompt"]
    for finding in (shared, left, right, dismissed):
        assert finding in merge_prompt, "All raw findings must reach the merge model for judgment"
    assert outcome != "merged" and case.tracker.merged_prs == []
    assert "blocked" in case.tracker.get_issue(1).labels
    blocked = [comment.body for comment in case.tracker.list_comments(1)
               if comment.body.startswith("<!-- foragent:blocked")]
    assert blocked, "Final merge blockers must be shown in the blocked issue comment"
    for finding in (shared, left, right):
        assert blocked[-1].count(finding) == 1, f"Final comment must contain each merged defect once: {finding}"
    assert dismissed not in blocked[-1], "A discarded reviewer claim leaked into the final blockers"


def test_review_parallel_failed_or_timed_out_reviewer_cannot_silently_pass(
    git_history, tmp_path, monkeypatch,
):
    results = []
    for mode in ("failed", "timed-out"):
        root = tmp_path / mode
        history = copy_history(git_history, root)
        case = perspective_case(history, root, monkeypatch, count=3, review_max=0, max_retries=1)
        # The first reviewer passes. Today's single-review path never observes
        # reviewer two's failed result and incorrectly merges the PR.
        panel = PanelExecutor(["VERDICT: PASS"] * len(CHECKLIST), failed=1, timed_out=mode == "timed-out", overlap=3)
        install_panel(case, panel)
        outcome = run_issue(1, case.ctx)
        results.append((mode, outcome, case, panel))
    for mode, outcome, case, panel in results:
        assert outcome != "merged" and case.tracker.merged_prs == [], (
            f"Parallel reviewer {mode} was silently ignored; outcome={outcome}"
        )
        assert len(panel.reviews) == len(CHECKLIST), "The failing reviewer must actually be invoked"
        assert not panel.overlap_errors
        assert "blocked" in case.tracker.get_issue(1).labels, "Use today's failed-review handling"


def test_review_parallel_non_risk_stays_single_and_risk_count_is_capped(
    git_history, tmp_path, monkeypatch,
):
    results = []
    for risk_paths, count, name in ((["docs/**"], 1, "ordinary"), (["feature*.txt"], len(CHECKLIST), "risk")):
        root = tmp_path / name
        history = copy_history(git_history, root)
        case = perspective_case(history, root, monkeypatch, count=9, risk_paths=risk_paths,
                                review_max=0, max_retries=1)
        panel = PanelExecutor(["VERDICT: PASS"] * count, overlap=count)
        install_panel(case, panel)
        results.append((run_issue(1, case.ctx), case, panel))
    ordinary_outcome, ordinary, single = results[0]
    assert ordinary_outcome == "merged" and ordinary.tracker.merged_prs
    assert len(single.calls) == len(single.reviews) == 1 and single.merges == []
    assert all(item["focus"] not in single.reviews[0]["prompt"] for item in CHECKLIST)
    risk_outcome, risk, capped = results[1]
    assert_panel(capped, len(CHECKLIST))
    assigned = []
    for call in capped.reviews:
        share = [item["name"] for item in CHECKLIST if item["focus"] in call["prompt"]]
        assert len(share) == 1, "Capped reviewers must each have one perspective"
        assigned.extend(share)
    assert sorted(assigned) == sorted(item["name"] for item in CHECKLIST)
    assert risk_outcome == "merged" and risk.tracker.merged_prs


def test_review_parallel_cli_issue_override_persists_wins_and_clear_restores_config(
    git_history, tmp_path, monkeypatch, capsys,
):
    from foragent.run_review import _review

    case = perspective_case(git_history, tmp_path, monkeypatch, count=2)
    main = bind_cli(case, monkeypatch)
    config_path = case.root / ".foragent.yml"
    config_bytes = config_path.read_bytes()
    cli_ok(main, ["orch", "reviewers", "1", "3"], capsys)

    def reopen_state():
        # Persistence is observable across store instances; the filename/schema
        # of the per-issue override is deliberately left to the implementation.
        case.store = case.ctx.store = StateStore(case.store.root, now=case.ctx.now)

    reopen_state()
    case.git.fetch("origin", "main")
    worktree = case.store.worktree_path(1)
    case.git.add_worktree(worktree, "foragent/issue-1", "origin/main")
    git = case.git.at(worktree)
    ScriptedExecutor().run(worktree=worktree, log_path=case.store.log_path(1))
    git.stage_changes()
    approval = case.store.load_approval(1)
    case.tracker.add_issue(7, "Unrelated issue", approval.body, labels=["approved", "running"])
    case.store.save_approval(**{**asdict(approval), "number": 7})
    case.store.start_run(7, branch="foragent/issue-7", worktree=worktree)

    def observe(number, expected_count):
        panel = PanelExecutor(["VERDICT: PASS"] * len(CHECKLIST), overlap=expected_count)
        install_panel(case, panel, implement=False)
        review = _review(number, case.ctx, git, parse_body(approval.body), "origin/main")
        assert_panel(panel, len(CHECKLIST))
        assert panel.maximum == expected_count, "The issue override limits concurrency, not perspective coverage"
        assert review.verdict == "PASS"

    observe(1, 3)
    observe(7, 2)
    for invalid in ("0", "-1"):
        assert cli_exit(main, ["orch", "reviewers", "1", invalid]) != 0
        capsys.readouterr()
    reopen_state()
    observe(1, 3)  # Rejected values must preserve the existing override.
    cli_ok(main, ["orch", "reviewers", "1", "--clear"], capsys)
    reopen_state()
    observe(1, 2)
    observe(7, 2)
    assert case.ctx.config.review_reviewers == 2
    assert config_path.read_bytes() == config_bytes, "Issue overrides belong in state, not project config"
