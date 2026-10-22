"""Acceptance test for changed-files-base. Locked by Foragent; do not edit."""

from test_skills_autosync import isolated_home


def test_changed_files_base_main_only_acceptance_publication_does_not_hold_fast_merge(git_history, tmp_path, monkeypatch):
    from foragent.policy import merge_decision
    from foragent.runner import run_issue
    from test_local_view import record_gh
    from test_orch_review_handoff import make_case

    record_gh(monkeypatch)
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true")
    other_order = "tests/acceptance/test_another_order.py"
    observed = []

    def implement_while_main_moves(kwargs):
        case.implement(kwargs)
        # Another order publishes while this branch still has its original base.
        # The fixture imports history locally; no commit/push/checkout is executed.
        case.history.update({other_order: "def test_another_order():\n    assert False\n"})
        case.git.fetch()
        branch_git = case.git.at(kwargs["worktree"])
        branch_git.stage_changes()
        # Exercise the public changed-file reader that supplies merge policy.
        observed.extend(branch_git.changed_paths("origin/main"))
        return "Implemented feature without touching acceptance tests"

    case.implementer.steps = [implement_while_main_moves]
    outcome = run_issue(1, case.ctx)
    decision = merge_decision(changed_paths=observed, issue_labels=case.tracker.get_issue(1).labels,
                              config=case.config)
    assert decision.kind == "merge", (
        f"Main-only acceptance publication is not a PR change: paths={observed}, "
        f"policy={decision.reason}, runner={outcome}"
    )
    assert observed == ["feature.txt"]
    assert outcome == "merged" and len(case.tracker.merged_prs) == 1
    assert not any("acceptance-touched" in comment.body for comment in case.tracker.list_comments(1))


def test_changed_files_base_real_pr_acceptance_change_is_still_held(git_history, tmp_path, monkeypatch):
    from foragent.runner import run_issue
    from test_local_view import record_gh
    from test_orch_review_handoff import make_case

    record_gh(monkeypatch)
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true")
    added = "tests/acceptance/test_pr_owned.py"
    create_pr = case.tracker.create_pr

    def create(**kwargs):
        number = create_pr(**kwargs)
        # Fake GitHub reports this PR's actual files, as its API would.
        case.tracker.set_pr_files(number, ["feature.txt", added])
        return number

    monkeypatch.setattr(case.tracker, "create_pr", create)

    def implement(kwargs):
        case.implement(kwargs)
        (kwargs["worktree"] / added).write_text("def test_pr_owned():\n    assert True\n")

    case.implementer.steps = [implement]
    assert run_issue(1, case.ctx) == "held"
    assert case.tracker.merged_prs == []
    assert any("reason=acceptance-touched" in comment.body for comment in case.tracker.list_comments(1))
