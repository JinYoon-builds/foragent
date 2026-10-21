"""Acceptance test for orch-blocked. Locked by Foragent; do not edit."""


def test_spec_question_is_blocked_and_hidden_from_human_inbox(git_history, tmp_path, monkeypatch):
    from foragent.inspect_data import InspectContext, collect
    from foragent.labels import LABELS
    from foragent.runner import run_issue
    from test_orch_review_handoff import make_case

    case = make_case(git_history, tmp_path, monkeypatch)

    def question(kwargs):
        path = kwargs["worktree"] / ".foragent/QUESTION.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text("Which locale?\nA: English (recommended)\nB: Korean\n")

    case.implementer.steps = [question]
    assert run_issue(1, case.ctx) == "spec-question"
    assert "blocked" in case.tracker.get_issue(1).labels
    assert "needs-human" not in case.tracker.get_issue(1).labels
    assert any(label.name == "blocked" for label in LABELS)
    assert case.tracker.list_comments(1)[-1].body.startswith(
        "<!-- foragent:blocked reason=spec-question -->")
    inbox = collect(InspectContext(case.tracker, case.store, "owner/repo", case.config,
                                   lambda: case.clock[0]))
    assert not inbox.items


def test_scheduler_neither_promotes_nor_starts_blocked_issue(git_history, tmp_path, monkeypatch):
    from foragent.scheduler import tick
    from test_orch_review_handoff import make_case, tick_context

    case = make_case(git_history, tmp_path, monkeypatch)
    case.store.finish_run(1, outcome="spec-question")
    case.tracker.remove_labels(1, ["running"])
    case.tracker.add_labels(1, ["blocked"])
    case.tracker.record_label(1, "approved", "human")
    launches = []
    ctx = tick_context(case)
    ctx.launcher = launches.append
    tick(ctx)
    assert not case.tracker.get_issue(1).labels & {"ready", "running"}
    assert launches == []


def test_timeout_uses_latest_blocked_comment_and_configured_clock(git_history, tmp_path, monkeypatch):
    from dataclasses import replace
    from datetime import timedelta

    from foragent.config import Config
    from foragent.scheduler import tick
    from test_orch_review_handoff import make_case, tick_context

    assert Config().orchestrator_timeout_minutes == 30
    case = make_case(git_history, tmp_path, monkeypatch)
    case.ctx.config = replace(case.config, orchestrator_timeout_minutes=5)
    case.store.finish_run(1, outcome="blocked")
    case.tracker.remove_labels(1, ["running"])
    case.tracker.add_labels(1, ["blocked"])
    case.tracker.comment(1, "<!-- foragent:blocked reason=spec-question -->\nOld question")
    case.clock[0] += timedelta(minutes=4)
    case.tracker.comment(1, "<!-- foragent:blocked reason=review-dispute -->\nNew dispute")
    case.clock[0] += timedelta(minutes=2)
    tick(tick_context(case))
    assert "needs-human" not in case.tracker.get_issue(1).labels
    case.clock[0] += timedelta(minutes=4)
    tick(tick_context(case))
    assert "needs-human" in case.tracker.get_issue(1).labels
    markers = [c for c in case.tracker.list_comments(1) if c.body.startswith(
        "<!-- foragent:needs-human reason=orchestrator-timeout -->")]
    assert len(markers) == 1
    tick(tick_context(case))
    assert len([c for c in case.tracker.list_comments(1) if c.body.startswith(
        "<!-- foragent:needs-human reason=orchestrator-timeout -->")]) == 1
