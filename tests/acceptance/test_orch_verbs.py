"""Acceptance test for orch-verbs. Locked by Foragent; do not edit."""

import pytest


def blocked_case(git_history, tmp_path, monkeypatch):
    from foragent.actions import flag
    from foragent.reasons import Reason
    from test_orch_review_handoff import bind_cli, make_case

    case = make_case(git_history, tmp_path, monkeypatch)
    case.store.finish_run(1, outcome="blocked")
    flag(case.ctx, 1, Reason.REVIEW_DISPUTE, "Locale mismatch", "Raw details")
    main = bind_cli(case, monkeypatch)
    monkeypatch.setattr("foragent.cli.RunContext", lambda **kwargs: case.ctx)
    return case, main


def assert_event(case):
    import json
    from datetime import datetime

    events = [json.loads(line) for line in (case.store.root / "orch-events.jsonl").read_text().splitlines()]
    event = events[-1]
    assert event["issue"] == 1
    assert event["kind"] and event["summary"]
    assert datetime.fromisoformat(event["at"]).tzinfo is not None
    assert any(c.body.startswith("<!-- foragent:orch -->") and event["summary"] in c.body
               for c in case.tracker.list_comments(1))
    assert not any(e.label == "approved" and e.actor != "human" for e in case.tracker.label_events(1))
    return event


def test_list_exposes_blocked_details_reviews_and_log_reference(git_history, tmp_path, monkeypatch, capsys):
    import json

    from foragent.orch import escalate

    case, main = blocked_case(git_history, tmp_path, monkeypatch)
    review = case.store.root / "runs/issue-1/review-1.txt"
    review.parent.mkdir(parents=True, exist_ok=True)
    review.write_text("Reviewer raw text: feature.txt:1 locale mismatch")
    log = case.store.log_path(1)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("".join(f"line {i}\n" for i in range(100)))
    case.tracker.add_issue(2, "Human decision")
    escalate(case.ctx, 2, "Choose a locale", "Reviewed the available locales")
    assert main(["orch", "list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    items = payload if isinstance(payload, list) else payload["items"]
    assert [item["number"] for item in items] == [1]
    item = items[0]
    assert item["title"] == "Implement feature" and item["lane"] == "main"
    assert item["reason"] == "review-dispute" and item["blocked_at"] == case.clock[0].isoformat()
    serialized = json.dumps(item, ensure_ascii=False)
    assert "Raw details" in serialized and "Reviewer raw text" in serialized
    assert any(str(path) in serialized for path in case.store.root.rglob("*") if path.is_file() and "log" in path.name)


def test_retry_closes_only_own_pr_and_consumes_hint_once(git_history, tmp_path, monkeypatch):
    from foragent.runner import run_issue

    case, main = blocked_case(git_history, tmp_path, monkeypatch)
    pr = case.tracker.create_pr(head="foragent/issue-1", base="main", title="Old attempt", body="Old")
    other = case.tracker.create_pr(head="other/work", base="main", title="Other", body="Other")
    hint = "Use the existing locale selection"
    assert main(["orch", "retry", "1", "--hint", hint]) == 0
    assert case.tracker.get_issue(pr).state == "closed"
    assert case.tracker.get_issue(other).state == "open"
    assert "blocked" not in case.tracker.get_issue(1).labels
    assert (case.store.root / "hints/1.txt").read_text().strip() == hint
    assert_event(case)

    def incomplete(kwargs):
        (kwargs["worktree"] / "draft.txt").write_text("draft")

    case.implementer.steps = [incomplete, case.implement]
    case.store.start_run(1, branch="foragent/issue-1", worktree=case.store.worktree_path(1))
    run_issue(1, case.ctx)
    assert len(case.implementer.calls) == 2
    assert "Orchestrator hint:" in case.implementer.calls[0]["prompt"]
    assert hint in case.implementer.calls[0]["prompt"]
    assert hint not in case.implementer.calls[1]["prompt"]
    assert not (case.store.root / "hints/1.txt").exists()
    assert "needs-human" not in case.tracker.get_issue(1).labels


def test_rerun_review_uses_current_pr_without_implementation(git_history, tmp_path, monkeypatch):
    from conftest import git_command

    case, main = blocked_case(git_history, tmp_path, monkeypatch)
    base = case.history.tip
    case.history.update({"feature.txt": "done"})
    pr = case.tracker.create_pr(head="foragent/issue-1", base="main", title="Existing", body="Keep this PR")
    case.tracker.set_pr_head_sha(pr, case.history.tip)
    case.tracker.set_pr_files(pr, ["feature.txt"])
    git_command(case.history.origin, "update-ref", "refs/heads/foragent/issue-1", case.history.tip)
    git_command(case.history.origin, "update-ref", "refs/heads/main", base)
    case.git.fetch()
    assert main(["orch", "rerun-review", "1"]) == 0
    assert case.implementer.calls == []
    assert len(case.reviewer.calls) == 1
    assert len(case.tracker.created_prs) == 1
    assert "blocked" not in case.tracker.get_issue(1).labels
    assert "needs-human" not in case.tracker.get_issue(1).labels
    assert case.tracker.merged_prs == [pr]
    assert_event(case)


@pytest.mark.parametrize("verdict,ci,feature", [("pass", "fail", True), ("pass", "pass", False),
                                                 ("fail", "pass", True)])
def test_resolve_records_head_but_cannot_override_checks(git_history, tmp_path, monkeypatch, verdict, ci, feature):
    from conftest import git_command

    case, main = blocked_case(git_history, tmp_path, monkeypatch)
    base = case.history.tip
    case.history.update({"feature.txt": "done"} if feature else {"draft.txt": "not done"})
    pr = case.tracker.create_pr(head="foragent/issue-1", base="main", title="Current", body="Current")
    case.tracker.set_pr_head_sha(pr, case.history.tip)
    case.tracker.set_pr_checks(pr, ci)
    git_command(case.history.origin, "update-ref", "refs/heads/foragent/issue-1", case.history.tip)
    git_command(case.history.origin, "update-ref", "refs/heads/main", base)
    case.git.fetch()
    assert main(["orch", "resolve", "1", "--verdict", verdict, "--note", "Checked the locale"]) == 0
    comments = case.tracker.list_comments(1) + case.tracker.list_comments(pr)
    marker = next(c.body for c in comments if c.body.startswith("<!-- foragent:orch-verdict"))
    assert case.history.tip in marker and verdict in marker and "Checked the locale" in marker
    assert "blocked" not in case.tracker.get_issue(1).labels
    assert case.tracker.merged_prs == []
    if verdict == "fail":
        assert case.tracker.get_issue(pr).state == "closed"
        assert "Checked the locale" in (case.store.root / "hints/1.txt").read_text()
    assert_event(case)


def test_escalate_records_options_and_log_keeps_latest_twenty(git_history, tmp_path, monkeypatch, capsys):
    import json
    import re

    case, main = blocked_case(git_history, tmp_path, monkeypatch)
    case.store.root.mkdir(parents=True, exist_ok=True)
    events = [{"at": case.clock[0].isoformat(), "issue": None, "kind": "prior",
               "summary": f"prior-event-{i:02d}"} for i in range(22)]
    (case.store.root / "orch-events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    assert main(["orch", "escalate", "1", "--summary", "Choose a locale", "--tried", "Reviewed twice",
                 "--option", "English", "--option", "Korean", "--recommend", "1"]) == 0
    assert "needs-human" in case.tracker.get_issue(1).labels
    text = next(c.body for c in case.tracker.list_comments(1) if c.body.startswith(
        "<!-- foragent:needs-human reason=escalated -->"))
    assert all(value in text for value in ("Choose a locale", "Reviewed twice", "English", "Korean"))
    assert any("English" in line and re.search(r"recommend|[⭐★✓✅]|\[[xX]\]", line, re.I)
               for line in text.splitlines())
    event = assert_event(case)
    capsys.readouterr()
    assert main(["orch", "log"]) == 0
    output = capsys.readouterr().out
    assert "prior-event-02" not in output and "prior-event-03" in output
    assert "prior-event-21" in output and event["summary"] in output
