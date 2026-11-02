"""Boundaries of the PASS scope the locked acceptance file leaves open (#1599)."""

from dataclasses import replace
from pathlib import Path
import sys

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "acceptance"))

from conftest import git_command
from foragent import orch_events, review_pass_scope, risks, run_review  # noqa: E402
from foragent.issue import body_hash  # noqa: E402
from foragent.run_review import _journal_review  # noqa: E402
from foragent.runner import review_only, run_issue  # noqa: E402
from foragent.state import StateStore  # noqa: E402
from test_order_machine_budget import fake_workloads  # noqa: F401
from test_review_pass_scope import (
    FIRST_FINDING, NEW_FINDING, OLD_FINDING, PASS, capture_review, case_for, content, implement,
)
from test_review_perspectives import review_offline  # noqa: F401
from test_risk_map import entry, offline  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401


FINISH = run_review._finish_review
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
    capture_review(case, [PASS] * 5)  # A mapped change also needs its perspectives and merger.
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
    # The FAIL leaves the first boundary, and the repair's review sees it with main's merged code.
    assert "-base = 0" in contexts[2] and "+retry = 3" in contexts[2]


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
    assert bound[0] in later[0] and "+from main" in later[0], "Main's merged code belongs to the later diff"


@pytest.mark.parametrize("reviewed_other", [False, True], ids=["no-review", "reviewed-under-other-body"])
def test_restored_body_keeps_the_pass_unless_a_review_ran_under_the_other_body(
    git_history, tmp_path, monkeypatch, reviewed_other,
):
    case = passed_case(git_history, tmp_path, monkeypatch)
    original = case.ctx.store.load_approval(1)
    edit_body(case, "Something else.")
    if reviewed_other:
        capture_review(case, ["VERDICT: FAIL\n" + OLD_FINDING])
        assert review_only(1, case.ctx) == "review-failed"
    case.tracker.edit_body(1, original.body)
    case.ctx.store.save_approval(**original.__dict__)
    later = capture_review(case, ["VERDICT: FAIL\n" + OLD_FINDING])
    review_only(1, case.ctx)
    assert (LIMITED in later[0]) is (not reviewed_other)
    assert ("+accepted = 'reviewed'" in later[0]) is reviewed_other


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


def anchored_case(git_history, tmp_path, monkeypatch, *, replies=("VERDICT: FAIL\n" + FIRST_FINDING, PASS), **settings):
    """FAIL then PASS on one body, published and held open, ready for review_only after a restart."""
    case = case_for(git_history, tmp_path, monkeypatch, review="careful", review_max=1, **settings)
    case.implementer.steps = [implement(content()), implement(content())]
    capture_review(case, list(replies))
    assert run_issue(1, case.ctx) == "pr-open"
    case.ctx = replace(case.ctx, store=StateStore(case.store.root, now=case.ctx.now),
                       config=replace(case.ctx.config, review="fast"))
    return case


def restore_body(case, approval):
    case.tracker.edit_body(1, approval.body)
    case.ctx.store.save_approval(**approval.__dict__)


def test_restored_body_after_a_review_elsewhere_drops_the_old_confirmation_anchor(git_history, tmp_path, monkeypatch):
    case = anchored_case(git_history, tmp_path, monkeypatch)
    original = case.ctx.store.load_approval(1)
    edit_body(case, "Something else.")
    other = capture_review(case, ["VERDICT: FAIL\n" + NEW_FINDING])
    assert review_only(1, case.ctx) == "review-failed"
    assert CONFIRMING not in other[0] and "+accepted = 'reviewed'" in other[0]
    restore_body(case, original)
    restored = capture_review(case, ["VERDICT: FAIL\n" + OLD_FINDING])
    assert review_only(1, case.ctx) == "review-failed"
    assert CONFIRMING not in restored[0] and FIRST_FINDING not in restored[0], "Old findings must not limit it"
    assert LIMITED not in restored[0] and "+accepted = 'reviewed'" in restored[0]
    # The next review confirms this full review's findings, never the older body's.
    following = capture_review(case, ["VERDICT: FAIL\n" + OLD_FINDING])
    review_only(1, case.ctx)
    assert CONFIRMING in following[0] and OLD_FINDING in following[0] and FIRST_FINDING not in following[0]


class Interrupted(KeyboardInterrupt):
    pass


@pytest.mark.parametrize("reviewer", ["codex", "claude"])
@pytest.mark.parametrize("stage", ["before-save", "after-save"])
def test_review_interrupted_under_another_body_still_ends_the_pass(
    git_history, tmp_path, monkeypatch, reviewer, stage,
):
    case = anchored_case(git_history, tmp_path, monkeypatch, reviewer=reviewer)
    original = case.ctx.store.load_approval(1)
    edit_body(case, "Something else.")

    def stop(*args, **kwargs):
        raise Interrupted

    if stage == "before-save":
        capture_review(case, [stop])
    else:
        capture_review(case, ["VERDICT: FAIL\n" + NEW_FINDING])
        monkeypatch.setattr("foragent.run_review._finish_review", stop)
    with pytest.raises(Interrupted):
        review_only(1, case.ctx)
    if stage == "after-save":
        monkeypatch.setattr("foragent.run_review._finish_review", FINISH)
    restore_body(case, original)
    case.ctx = replace(case.ctx, store=StateStore(case.store.root, now=case.ctx.now))
    calls = []
    later = capture_review(case, ["VERDICT: FAIL\n" + OLD_FINDING], calls=calls)
    assert review_only(1, case.ctx) == "review-failed"
    assert LIMITED not in later[0] and CONFIRMING not in later[0] and NEW_FINDING not in later[0]
    if reviewer == "claude":
        assert calls[0].get("review_level") is not None, "Today's native full review"
    else:
        assert "+accepted = 'reviewed'" in later[0]


def test_panel_interrupted_under_another_body_still_ends_the_pass(git_history, tmp_path, monkeypatch):
    git_history.update({".foragent-risks.yml": yaml.safe_dump([entry(["feature.txt"])])})
    case = anchored_case(git_history, tmp_path, monkeypatch, replies=["VERDICT: FAIL\n" + FIRST_FINDING] * 5 + [PASS])
    original = case.ctx.store.load_approval(1)
    edit_body(case, "Something else.")
    capture_review(case, ["VERDICT: FAIL\n" + NEW_FINDING] * 5)

    def stop(*args, **kwargs):
        raise Interrupted

    monkeypatch.setattr("foragent.run_review._finish_review", stop)
    with pytest.raises(Interrupted):
        review_only(1, case.ctx)
    monkeypatch.setattr("foragent.run_review._finish_review", FINISH)
    restore_body(case, original)
    case.ctx = replace(case.ctx, store=StateStore(case.store.root, now=case.ctx.now))
    later = capture_review(case, ["VERDICT: FAIL\n" + OLD_FINDING] * 5)
    assert review_only(1, case.ctx) == "review-failed"
    assert later and all(LIMITED not in text and "+accepted = 'reviewed'" in text for text in later)


def test_interrupted_review_under_the_same_body_keeps_the_pass(git_history, tmp_path, monkeypatch):
    case = anchored_case(git_history, tmp_path, monkeypatch)

    def stop(*args, **kwargs):
        raise Interrupted

    capture_review(case, [stop])
    with pytest.raises(Interrupted):
        review_only(1, case.ctx)
    case.ctx = replace(case.ctx, store=StateStore(case.store.root, now=case.ctx.now))
    later = capture_review(case, ["VERDICT: PASS"])
    review_only(1, case.ctx)
    assert LIMITED in later[0]


def test_pass_belongs_to_the_body_it_started_under_not_a_mid_review_edit(git_history, tmp_path, monkeypatch):
    case = anchored_case(git_history, tmp_path, monkeypatch)
    original = case.ctx.store.load_approval(1)

    def edit_during_review(call):
        edit_body(case, "Something else.")
        return "VERDICT: PASS"

    capture_review(case, [edit_during_review])
    review_only(1, case.ctx)
    newest = orch_events.events(case.ctx.store, "review")[-1]
    assert newest["verdict"] == "PASS" and newest["body_sha"] == original.body_sha
    assert not review_pass_scope.applies(1, case.ctx, case.ctx.git), "No reviewer accepted the edited body"


def test_orchestrator_verdict_under_another_body_also_ends_the_pass(git_history, tmp_path, monkeypatch):
    case = anchored_case(git_history, tmp_path, monkeypatch)
    original = case.ctx.store.load_approval(1)
    edit_body(case, "Something else.")
    case.ctx = replace(case.ctx, config=replace(case.ctx.config, review="careful"))
    review_only(1, case.ctx, verdict_head=case.tracker.pr_head_sha(2))
    restore_body(case, original)
    later = capture_review(case, ["VERDICT: FAIL\n" + OLD_FINDING])
    review_only(1, case.ctx)
    assert LIMITED not in later[0] and CONFIRMING not in later[0] and "+accepted = 'reviewed'" in later[0]


@pytest.mark.parametrize("lost", ["commit", "record"])
def test_lost_boundary_falls_back_to_full_not_to_the_old_confirmation(git_history, tmp_path, monkeypatch, lost):
    case = anchored_case(git_history, tmp_path, monkeypatch)
    if lost == "commit":
        monkeypatch.setattr(review_pass_scope, "_exists", lambda git, commit: False)
    else:
        (case.store.root / "runs/issue-1/review-2.txt").unlink()
    later = capture_review(case, ["VERDICT: FAIL\n" + OLD_FINDING])
    assert review_only(1, case.ctx) == "review-failed"
    assert LIMITED not in later[0] and CONFIRMING not in later[0] and "+accepted = 'reviewed'" in later[0]


def test_exempt_change_keeps_todays_confirmation_with_the_pass_evidence(git_history, tmp_path, monkeypatch):
    git_history.update({".foragent-risks.yml": yaml.safe_dump([entry(["feature.txt"], pass_scope_exempt=True)])})
    # A mapped first review is a panel: four perspectives and a merger.
    case = anchored_case(git_history, tmp_path, monkeypatch, replies=["VERDICT: FAIL\n" + FIRST_FINDING] * 5 + [PASS])
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
    git._call(["merge", "--quiet", "--no-edit", "origin/main"])
    boundary = review_pass_scope.for_review(1, case.ctx, git, "origin/main")
    assert boundary.diff is None and "upstream.txt" in boundary.section


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
