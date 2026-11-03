"""Acceptance test for skills-review-scope. Locked by Foragent; do not edit."""


def scope_run(git_history, tmp_path, monkeypatch, *, mode=None, failing_review=False):
    import json

    from foragent import executors
    from foragent.runner import run_issue
    from test_orch_review_handoff import make_case

    options = {"review_scope": mode} if mode is not None else {}
    case = make_case(git_history, tmp_path, monkeypatch, executor="codex", reviewer="claude", **options)
    observations = []
    results = ["feature.txt:1 — Previous locale finding", "(none)"] if failing_review else ["(none)"]
    case.implementer.steps = [case.implement] * len(results)

    def transport(argv, **kwargs):
        observations.append({"argv": argv, "prs": list(case.tracker.created_prs),
                             "scope": (kwargs["cwd"] / "CLAUDE.local.md").read_text()
                             if (kwargs["cwd"] / "CLAUDE.local.md").is_file() else ""})
        result = results.pop(0)
        native = argv[argv.index("-p") + 1].startswith("/code-review")  # A confirmation round uses the prompt path.
        kwargs["stdout_path"].write_text(json.dumps(
            {"result": result} if native else {"type": "result", "result": "VERDICT: PASS"}))
        return 0, False, ""

    monkeypatch.setattr(executors, "run_logged", transport)
    case.ctx.executor_factory = lambda name: case.implementer if name == "codex" else executors.ClaudeExecutor()
    outcome = run_issue(1, case.ctx)
    return case, observations, outcome


def test_pr_body_mode_opens_pr_before_review_and_passes_number_in_argv(git_history, tmp_path, monkeypatch):
    case, calls, outcome = scope_run(git_history, tmp_path, monkeypatch, mode="pr-body")
    assert outcome == "merged"
    assert len(calls) == 1 and len(calls[0]["prs"]) == 1
    pr = calls[0]["prs"][0]
    assert calls[0]["argv"][calls[0]["argv"].index("-p") + 1] == f"/code-review {pr.number} low"
    scope = pr.body
    assert "## Review scope" in scope
    assert all(value in scope for value in ("Goal", "Out of scope", "weakened", "tests/acceptance/", "other issues"))


def test_previous_findings_are_comments_and_do_not_pollute_pr_review_scope(git_history, tmp_path, monkeypatch):
    case, calls, outcome = scope_run(git_history, tmp_path, monkeypatch, mode="pr-body", failing_review=True)
    assert outcome == "merged" and len(calls) == 2
    pr = calls[1]["prs"][0]
    assert "## Review scope" in pr.body and "Previous locale finding" not in pr.body
    assert any("Previous locale finding" in comment.body for comment in case.tracker.list_comments(pr.number))
    assert "Previous locale finding" not in case.tracker.get_issue(pr.number).body


def test_default_keeps_local_scope_and_existing_review_argv(git_history, tmp_path, monkeypatch):
    case, calls, outcome = scope_run(git_history, tmp_path, monkeypatch)
    assert outcome == "merged"
    assert len(calls) == 1 and calls[0]["prs"] == []
    assert "Goal" in calls[0]["scope"] and "tests/acceptance/" in calls[0]["scope"]
    assert calls[0]["argv"][calls[0]["argv"].index("-p") + 1] == "/code-review low"
