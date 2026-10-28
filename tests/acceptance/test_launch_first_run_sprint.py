"""Launch C2: offer a starter from repository facts, with no real PR or LLM."""

import re
from types import SimpleNamespace

from test_init import setup
from test_init_role_agents import init_project
from test_skills_autosync import cli_exit, isolated_home, write_global_config


def test_init_offers_one_ten_minute_starter_only_without_foragent_work(
    setup, init_project, monkeypatch, tmp_path, capsys,
):
    from foragent import cli, init, tracker as tracker_module
    from foragent.init import init_repo
    from foragent.tracker import FakeTracker

    write_global_config({"skill_libraries": [], "upstream_reports": False})
    observations = {}
    for case in ("empty", "open-approved", "merged-foragent", "unapproved-only", "unrelated-merged"):
        root = tmp_path / case
        root.mkdir()
        tracker = FakeTracker()
        if case in {"open-approved", "unapproved-only"}:
            tracker.add_issue(7, "Existing task", labels=["approved"] if case == "open-approved" else [])
        if case in {"merged-foragent", "unrelated-merged"}:
            foragent = case == "merged-foragent"
            tracker.add_issue(7, "Finished task", state="closed")
            pr = tracker.create_pr(head="foragent/issue-7" if foragent else "human/readme",
                                   base="main", title="Finished task", body="Closes #7")
            tracker.merge_pr(pr, expected_head_sha=tracker.pr_head_sha(pr))
        issues_before = tracker.list_issues()
        prs_before = [*tracker.list_prs(state="open"), *tracker.list_prs(state="merged")]
        commits, prompts = [], []
        git = SimpleNamespace(
            check_worktree=lambda: None, worktree_root=lambda: root.resolve(),
            remote_slug=lambda: "owner/repo", uncommitted_paths=lambda paths: [],
            current_branch=lambda: "main", commit_paths=lambda *args: commits.append(args),
        )

        def answer(prompt=""):
            prompts.append(prompt)
            return "n"  # An offer is not permission to create/approve a starter.

        with monkeypatch.context() as patch:
            patch.setattr("builtins.input", answer)
            patch.setattr(cli, "init_repo", lambda selected, **kw: init_repo(
                selected, git=git, gh_run=setup.run_gh, tracker=tracker, **kw))
            for module in (cli, init, tracker_module):
                patch.setattr(module, "GhTracker", lambda *a, **kw: tracker)
            capsys.readouterr()
            code = cli_exit(cli.main, ["init", "--repo-root", str(root), "--no-push", "--no-launchd",
                                       "--no-board", "--statusline", "no", "--language", "en"])
            captured = capsys.readouterr()
        assert code == 0, f"Offline init must work for {case}: {captured.out}{captured.err}"
        prs_after = [*tracker.list_prs(state="open"), *tracker.list_prs(state="merged")]
        assert tracker.list_issues() == issues_before and prs_after == prs_before, (
            "Declining a starter must not publish, approve or merge work")
        output = captured.out + captured.err + "\n" + "\n".join(prompts)
        offered = "starter order" in output.lower()
        if offered:
            assert re.search(r"\b10\s*(?:minutes|min)\b", output, re.I), "Explain the approximate ten-minute goal"
            assert len([p for p in prompts if "starter order" in p.lower()]) <= 1, "Offer one starter, not a repeated interview"
        observations[case] = offered
    # Evaluate all cases even on today's implementation, which offers nothing.
    assert observations == {"empty": True, "open-approved": False, "merged-foragent": False,
                            "unapproved-only": True, "unrelated-merged": True}, (
        f"Starter offer must follow approved backlog and merged Foragent PR facts: {observations}")
