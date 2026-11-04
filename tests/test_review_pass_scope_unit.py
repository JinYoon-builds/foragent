"""Boundaries of the PASS scope the locked acceptance file leaves open (#1599)."""

from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from conftest import git_command
from foragent import orch_events, review_pass_scope, risks
from foragent.issue import body_hash
from foragent.run_review import _journal_review
from foragent.runner import review_only, run_issue
from foragent.state import StateStore
from helpers.workloads import fake_workloads  # noqa: F401
from helpers.review_scope import (
    FIRST_FINDING, NEW_FINDING, OLD_FINDING, PASS, capture_review, case_for, content, implement,
)
from foragent.review_perspectives import DEFAULT_PERSPECTIVES
from runner_support import GROUNDED_SCORE, scoring_request
from helpers.review_panel import review_offline  # noqa: F401
from helpers.risk import entry, offline  # noqa: F401
from helpers.home import isolated_home  # noqa: F401

LIMITED = "Limit blocking findings to code changed after that PASS"


def conflict_case(git_history, tmp_path, monkeypatch, **settings):
    """The first review's PASS, with main moving so publication hits a real conflict."""
    case = case_for(git_history, tmp_path, monkeypatch, **settings)

    def first_review(call):
        case.history.update({"feature.txt": content("upstream", 0, 1)})
        return PASS

    return case, first_review


def passed_case(git_history, tmp_path, monkeypatch, **settings):
    """One published PASS held open for a person, ready for review_only in a new store."""
    case = case_for(git_history, tmp_path, monkeypatch, review="careful", **settings)
    case.implementer.steps = [implement(content())]
    capture_review(case, [PASS] * (len(DEFAULT_PERSPECTIVES) + 1))  # A mapped change also needs its perspectives and merger.
    assert run_issue(1, case.ctx) == "pr-open"
    case.ctx = replace(case.ctx, store=StateStore(case.store.root, now=case.ctx.now),
                       config=replace(case.ctx.config, review="fast"))
    return case


def edit_body(case, text):
    store = case.ctx.store
    approval = store.load_approval(1)
    body = approval.body.replace("Implement the feature.", text) if text else approval.body
    case.tracker.edit_body(1, body)
    store.save_approval(**{**approval.__dict__, "body": body, "body_sha": body_hash(body)})
    return approval


def test_off_switch_removes_the_extra_conflict_review(git_history, tmp_path, monkeypatch):
    case, first_review = conflict_case(git_history, tmp_path, monkeypatch, review_pass_scope=False)
    case.implementer.steps = [implement(content()), implement(content(retry=2, base=1))]
    contexts = capture_review(case, [first_review])
    assert run_issue(1, case.ctx) == "merged"
    assert len(case.implementer.calls) == 2, "Exercise actual conflict resolution"
    assert len(contexts) == 1


def test_conflict_finding_within_budget_returns_to_the_lane_then_merges(git_history, tmp_path, monkeypatch):
    case, first_review = conflict_case(git_history, tmp_path, monkeypatch, review_max=1)
    case.implementer.steps = [implement(content()), implement(content(retry=2, base=1)),
                              implement(content(retry=3, base=1))]
    contexts = capture_review(case, [first_review, "VERDICT: FAIL\n" + NEW_FINDING, "VERDICT: PASS"])
    assert run_issue(1, case.ctx) == "merged"
    assert len(case.implementer.calls) == 3 and len(contexts) == 3
    assert NEW_FINDING in case.implementer.calls[2]["prompt"]
    # The FAIL leaves the first boundary; the repair's review sees this PR's work, not main's merged code.
    assert "-base = 0" not in contexts[2] and "+base = 1" not in contexts[2] and "+retry = 3" in contexts[2]


def test_conflict_at_a_later_base_move_can_still_hold(git_history, tmp_path, monkeypatch):
    from foragent import run_publish

    case = case_for(git_history, tmp_path, monkeypatch)
    original = run_publish._merge_decision

    def main_moves_after_the_pr_opens(*args, **kwargs):
        case.history.update({"feature.txt": content("upstream", 0, 1)})
        monkeypatch.setattr(run_publish, "_merge_decision", original)
        return original(*args, **kwargs)

    monkeypatch.setattr(run_publish, "_merge_decision", main_moves_after_the_pr_opens)
    case.implementer.steps = [implement(content()), implement(content(retry=2, base=1))]
    contexts = capture_review(case, [PASS, "VERDICT: FAIL\n" + NEW_FINDING])
    assert run_issue(1, case.ctx) == "held"
    assert len(contexts) == 2 and LIMITED in contexts[1] and not case.tracker.merged_prs
    returns = [c.body for c in case.tracker.list_comments(1) if c.body.startswith("<!-- foragent:blocked")]
    assert len(returns) == 1 and NEW_FINDING in returns[0]


def test_clean_base_update_adds_no_review_and_never_moves_the_boundary(git_history, tmp_path, monkeypatch):
    case = case_for(git_history, tmp_path, monkeypatch, review="careful")

    def first_review(call):
        case.history.update({"upstream.txt": "from main\n"})
        return PASS

    case.implementer.steps = [implement(content())]
    contexts = capture_review(case, [first_review])
    assert run_issue(1, case.ctx) == "pr-open"
    assert len(contexts) == 1 and len(case.implementer.calls) == 1
    published = case.tracker.pr_head_sha(2)
    bound = [event["head"] for event in orch_events.events(case.ctx.store, review_pass_scope.BOUND)]
    assert len(bound) == 1 and bound[0] != published
    assert git_command(case.history.origin, "show", f"{published}:upstream.txt") == "from main"
    case.ctx = replace(case.ctx, store=StateStore(case.store.root, now=case.ctx.now),
                       config=replace(case.config, review="fast"))
    later = capture_review(case, ["VERDICT: FAIL\n" + NEW_FINDING])
    assert review_only(1, case.ctx) == "review-failed"
    assert bound[0] in later[0], "The later review keeps the PASS head"
    assert "upstream.txt" not in later[0] and "+from main" not in later[0], "Main's merged code stays out"


def test_new_pass_after_a_body_edit_sets_a_new_boundary(git_history, tmp_path, monkeypatch):
    case = passed_case(git_history, tmp_path, monkeypatch)
    first = case.tracker.pr_head_sha(2)
    edit_body(case, "Something else.")
    capture_review(case, ["VERDICT: PASS\nNOTE: new-record"])
    case.ctx = replace(case.ctx, config=replace(case.ctx.config, review="careful"))
    review_only(1, case.ctx)
    later = capture_review(case, ["VERDICT: PASS"])
    review_only(1, case.ctx)
    assert LIMITED in later[0] and "new-record" in later[0] and "PASS-RECEIPT-47" not in later[0]
    assert first in later[0], "The same commit was reviewed again; the boundary is that PASS"


@pytest.mark.parametrize("verdict, source", [
    ("PASS", "orchestrator"), ("UNUSABLE", "reviewer"), ("FAIL", "reviewer"),
], ids=["orchestrator", "unusable", "fail"])
def test_only_a_real_reviewer_pass_makes_a_boundary(git_history, tmp_path, monkeypatch, verdict, source):
    case = case_for(git_history, tmp_path, monkeypatch, review="careful")
    case.implementer.steps = [implement(content())]
    # Publish without a reviewer, then journal the one kind of round this case proves is no boundary.
    case.ctx = replace(case.ctx, config=replace(case.ctx.config, reviewer="none"))
    assert run_issue(1, case.ctx) == "pr-open"
    case.ctx = replace(case.ctx, config=replace(case.config, review="fast", reviewer="codex"))
    git_head = case.tracker.pr_head_sha(2)
    (case.store.root / "runs/issue-1").mkdir(parents=True, exist_ok=True)
    (case.store.root / "runs/issue-1/review-1.txt").write_text(PASS)
    tree = git_command(case.history.origin, "rev-parse", f"{git_head}^{{tree}}")
    _journal_review(1, case.ctx, verdict, "", 1, source=source, tree=tree, head=git_head,
                    body_sha=case.store.load_approval(1).body_sha)
    later = capture_review(case, ["VERDICT: FAIL\n" + OLD_FINDING])
    assert review_only(1, case.ctx) == "review-failed"
    assert LIMITED not in later[0] and "+accepted = 'reviewed'" in later[0]


CONFIRMING = "Limit this review to confirming"


def scored(texts):
    """Reviewer replies in order; every scoring session (#1651) grounds the scripted FAIL instead."""
    texts = iter(texts)

    def reply(call):
        return GROUNDED_SCORE if scoring_request(call["prompt"]) else next(texts)

    return [reply] * 32


def anchored_case(git_history, tmp_path, monkeypatch, *, replies=("VERDICT: FAIL\n" + FIRST_FINDING, PASS), **settings):
    """FAIL then PASS on one body, published and held open, ready for review_only after a restart."""
    case = case_for(git_history, tmp_path, monkeypatch, review="careful", review_max=1, **settings)
    case.implementer.steps = [implement(content()), implement(content())]
    capture_review(case, list(replies))
    assert run_issue(1, case.ctx) == "pr-open"
    case.ctx = replace(case.ctx, store=StateStore(case.store.root, now=case.ctx.now),
                       config=replace(case.ctx.config, review="fast"))
    return case


def test_exempt_change_keeps_todays_confirmation_with_the_pass_evidence(git_history, tmp_path, monkeypatch):
    git_history.update({".foragent-risks.yml": yaml.safe_dump([entry(["feature.txt"], pass_scope_exempt=True)])})
    # A mapped first review is a panel: four perspectives and a merger.
    case = anchored_case(git_history, tmp_path, monkeypatch, replies=scored(["VERDICT: FAIL\n" + FIRST_FINDING] * (len(DEFAULT_PERSPECTIVES) + 1) + [PASS]))
    head = case.tracker.pr_head_sha(2)
    exempt = capture_review(case, ["VERDICT: PASS"])
    review_only(1, case.ctx)
    assert CONFIRMING in exempt[0] and FIRST_FINDING in exempt[0]
    assert head in exempt[0] and "pass_scope_exempt" in exempt[0]


def test_off_switch_keeps_todays_confirmation_after_a_pass(git_history, tmp_path, monkeypatch):
    case = anchored_case(git_history, tmp_path, monkeypatch)
    case.ctx = replace(case.ctx, config=replace(case.ctx.config, review_pass_scope=False))
    later = capture_review(case, ["VERDICT: PASS"])
    review_only(1, case.ctx)
    assert CONFIRMING in later[0] and FIRST_FINDING in later[0] and LIMITED not in later[0]


def test_later_fail_on_the_same_body_leaves_the_boundary(git_history, tmp_path, monkeypatch):
    case = passed_case(git_history, tmp_path, monkeypatch)
    head = case.tracker.pr_head_sha(2)
    for _ in range(2):
        later = capture_review(case, ["VERDICT: FAIL\n" + NEW_FINDING])
        assert review_only(1, case.ctx) == "review-failed"
        assert head in later[0] and LIMITED in later[0]


def test_missing_pass_commit_or_record_falls_back_to_full_review(git_history, tmp_path, monkeypatch):
    case = passed_case(git_history, tmp_path, monkeypatch)
    for path in (case.store.root / "runs/issue-1").glob("review-*.txt"):
        path.unlink()
    later = capture_review(case, ["VERDICT: FAIL\n" + OLD_FINDING])
    assert review_only(1, case.ctx) == "review-failed"
    assert LIMITED not in later[0] and "+accepted = 'reviewed'" in later[0]


def test_unbound_pass_never_names_a_head_and_binding_needs_the_same_tree(git_history, tmp_path, monkeypatch):
    case = passed_case(git_history, tmp_path, monkeypatch)
    git = case.ctx.git
    assert review_pass_scope.applies(1, case.ctx, git)
    event = orch_events.events(case.ctx.store, "review")[-1]
    assert event["verdict"] == "PASS" and event["source"] == "reviewer"
    head = case.tracker.pr_head_sha(2)
    assert orch_events.events(case.ctx.store, review_pass_scope.BOUND)[-1]["head"] == head
    # A later PASS whose tree no commit has yet is not bound by an unrelated HEAD.
    _journal_review(1, case.ctx, "PASS", "", 2, tree="0" * 40, body_sha=event["body_sha"])
    before = len(orch_events.events(case.ctx.store, review_pass_scope.BOUND))
    review_pass_scope.bind(1, case.ctx, git)
    assert len(orch_events.events(case.ctx.store, review_pass_scope.BOUND)) == before
    # The search goes back to the earlier bound PASS instead of inventing a head.
    (case.store.root / "runs/issue-1/review-2.txt").write_text(PASS)
    assert review_pass_scope._latest_pass(1, case.ctx, git)[0] == head


@pytest.mark.parametrize("value, exempt", [(True, True), ("true", False), (1, False), (False, False)])
def test_only_a_yaml_true_mark_is_an_exemption(git_history, tmp_path, monkeypatch, value, exempt):
    git_history.update({".foragent-risks.yml": yaml.safe_dump([entry(["feature.txt"], pass_scope_exempt=value)])})
    case = passed_case(git_history, tmp_path, monkeypatch)
    boundary = review_pass_scope.for_review(1, case.ctx, _at_head(case, tmp_path), "origin/main")
    assert (boundary.diff is None) is exempt


def test_mark_added_only_on_the_branch_grants_nothing(git_history, tmp_path, monkeypatch):
    git_history.update({".foragent-risks.yml": yaml.safe_dump([entry(["feature.txt"])])})
    case = passed_case(git_history, tmp_path, monkeypatch)
    git = _at_head(case, tmp_path)
    (git.root / ".foragent-risks.yml").write_text(yaml.safe_dump([entry(["feature.txt"], pass_scope_exempt=True)]))
    git._call(["add", ".foragent-risks.yml"])
    assert review_pass_scope.for_review(1, case.ctx, git, "origin/main").diff is not None


def test_exemption_also_matches_a_path_brought_in_from_main(git_history, tmp_path, monkeypatch):
    git_history.update({".foragent-risks.yml": yaml.safe_dump([entry(["upstream.txt"], pass_scope_exempt=True)])})
    case = passed_case(git_history, tmp_path, monkeypatch)
    git = _at_head(case, tmp_path)
    assert review_pass_scope.for_review(1, case.ctx, git, "origin/main").diff is not None
    case.history.update({"upstream.txt": "from main\n"})
    git._call(["fetch", "--quiet", "origin"])
    git._call(["-c", "user.name=Tests", "-c", "user.email=tests@example.com",
               "merge", "--quiet", "--no-edit", "origin/main"])
    boundary = review_pass_scope.for_review(1, case.ctx, git, "origin/main")
    # A file only main changed gains no PR scope, so its exempt mark does not apply (2026-11-02 decision).
    assert boundary.diff is not None and "upstream.txt" not in boundary.diff
    assert "upstream.txt" not in boundary.section


def _at_head(case, tmp_path):
    worktree = tmp_path / "review-at-head"
    case.ctx.git.fetch("origin", "foragent/issue-1")
    case.ctx.git.add_detached_worktree(worktree, case.tracker.pr_head_sha(2))
    return case.ctx.git.at(worktree)


def test_scoped_note_reaches_the_issue_once(git_history, tmp_path, monkeypatch):
    case = passed_case(git_history, tmp_path, monkeypatch)
    for _ in range(2):
        capture_review(case, ["VERDICT: FAIL\n" + NEW_FINDING + "\nNOTE: " + OLD_FINDING])
        assert review_only(1, case.ctx) == "review-failed"
    comments = [c.body for c in case.tracker.list_comments(1) if c.body.startswith("Review notes")]
    assert len(comments) == 1 and "NOTE: " + OLD_FINDING in comments[0]


def test_off_keeps_notes_where_they_were_and_full_review_prompt(git_history, tmp_path, monkeypatch):
    case = passed_case(git_history, tmp_path, monkeypatch)
    case.ctx = replace(case.ctx, config=replace(case.ctx.config, review_pass_scope=False))
    later = capture_review(case, ["VERDICT: FAIL\n" + OLD_FINDING + "\nNOTE: elsewhere"])
    assert review_only(1, case.ctx) == "review-failed"
    assert "review_pass_scope" not in later[0] and "+accepted = 'reviewed'" in later[0]
    assert not [c for c in case.tracker.list_comments(1) if c.body.startswith("Review notes")]


def test_body_edit_before_any_pass_keeps_todays_confirmation(git_history, tmp_path, monkeypatch):
    case = case_for(git_history, tmp_path, monkeypatch, review="careful")
    case.implementer.steps = [implement(content())]
    capture_review(case, ["VERDICT: FAIL\n" + FIRST_FINDING])
    assert run_issue(1, case.ctx) == "held"
    edit_body(case, "Something else.")
    later = capture_review(case, ["VERDICT: PASS"])
    review_only(1, case.ctx)
    assert "Limit this review to confirming" in later[0] and FIRST_FINDING in later[0]


def test_review_events_record_the_body_and_only_a_pass_records_a_tree(git_history, tmp_path, monkeypatch):
    case = case_for(git_history, tmp_path, monkeypatch, review="careful", review_max=1)
    case.implementer.steps = [implement(content()), implement(content())]
    capture_review(case, ["VERDICT: FAIL\n" + FIRST_FINDING, PASS])
    assert run_issue(1, case.ctx) == "pr-open"
    failed, passed = orch_events.events(case.ctx.store, "review")
    body_sha = case.store.load_approval(1).body_sha
    assert failed["body_sha"] == passed["body_sha"] == body_sha
    assert "tree" not in failed and "head" not in failed and passed["tree"]


def test_risk_entry_and_cli_write_and_show_the_mark(git_history, tmp_path, monkeypatch, capsys):
    from foragent.cli import main

    arguments = ["risk", "add", "--repo-root", str(git_history.clone), "--path", "src/a.py", "--why", "w",
                 "--incident-date", "2026-11-02", "--incident", "i", "--guidance", "g", "--by", "me"]
    assert main([*arguments, "--pass-scope-exempt"]) == 0
    assert main(arguments) == 0
    marked, plain = risks.load(git_history.clone)
    assert marked["pass_scope_exempt"] is True and "pass_scope_exempt" not in plain
    capsys.readouterr()
    assert main(["risk", "list", "--repo-root", str(git_history.clone)]) == 0
    first, second = capsys.readouterr().out.splitlines()
    assert "(full review after a PASS)" in first and "(full review after a PASS)" not in second


def test_incident_entries_of_this_repository_carry_the_mark():
    entries = risks.load(Path(__file__).parents[1])
    assert entries and all(risk.get("pass_scope_exempt") is True for risk in entries)
