"""Acceptance test for orch-skill. Locked by Foragent; do not edit."""


def test_watch_emits_for_blocked_human_merge_and_queue_empty_changes(git_history, tmp_path, monkeypatch, capsys):
    from datetime import timedelta
    import json

    from foragent.actions import flag
    from foragent.orch import escalate
    from foragent.reasons import Reason
    from foragent.scheduler import tick
    from test_orch_review_handoff import bind_cli, make_case, tick_context

    case = make_case(git_history, tmp_path, monkeypatch)
    ctx = tick_context(case, start_runs=False)
    tick(ctx)
    main = bind_cli(case, monkeypatch)

    def changed():
        assert main(["orch", "watch", "--interval", "0.01"]) == 0
        lines = capsys.readouterr().out.splitlines()
        assert len(lines) == 1
        assert isinstance(json.loads(lines[0]), dict)
        assert (case.store.root / "orch-watch.json").is_file()
        return lines[0]

    baseline = changed()
    flag(case.ctx, 1, Reason.SPEC_QUESTION, "Choose a locale")
    case.store.finish_run(1, outcome="spec-question")
    blocked = changed()
    assert json.loads(blocked)["blocked"] == [1]
    escalate(case.ctx, 1, "Choose a locale", "Reviewed the available locales")
    human = changed()
    assert json.loads(human)["needs_human"] == [1]
    # A person merges and then closes on GitHub; each becomes visible after
    # the gate's next eligible refresh, unlike Foragent's writes above.
    pr = case.tracker.create_pr(head="foragent/issue-1", base="main", title="Feature", body="Closes #1")
    case.tracker.merge_pr(pr, expected_head_sha=case.tracker.pr_head_sha(pr))
    case.clock[0] += timedelta(minutes=5)
    tick(ctx)
    merged = changed()
    assert json.loads(merged)["merged"] == [1]
    case.tracker.close_issue(1)
    case.clock[0] += timedelta(minutes=5)
    tick(ctx)
    empty = changed()
    assert json.loads(empty)["queue_empty"] is True
    assert len({baseline, blocked, human, merged, empty}) == 5


def test_watch_waits_through_identical_polls_and_ignores_title_edits(git_history, tmp_path, monkeypatch, capsys):
    from datetime import timedelta
    import json
    import time

    from foragent.actions import flag
    from foragent.reasons import Reason
    from foragent.scheduler import tick
    from test_orch_review_handoff import bind_cli, make_case, tick_context

    case = make_case(git_history, tmp_path, monkeypatch)
    ctx = tick_context(case, start_runs=False)
    tick(ctx)
    main = bind_cli(case, monkeypatch)
    assert main(["orch", "watch", "--interval", "0.01"]) == 0
    capsys.readouterr()
    polls = []

    def sleep(seconds):
        polls.append(seconds)
        assert capsys.readouterr().out == "", "An unchanged watch must not emit"
        assert len(polls) <= 3, "Watch did not observe an injected change"
        if len(polls) == 1:
            issue = case.tracker.get_issue(1)
            case.tracker.update_issue(1, title="Title only", body=issue.body)
            # Import the person's title edit so the next unchanged poll
            # really proves that titles are excluded from watch events.
            case.clock[0] += timedelta(minutes=5)
            tick(ctx)
        if len(polls) == 2:
            flag(case.ctx, 1, Reason.SPEC_QUESTION, "Choose a locale")

    monkeypatch.setattr(time, "sleep", sleep)
    assert main(["orch", "watch", "--interval", "0.01"]) == 0
    assert len(polls) >= 2
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1 and isinstance(json.loads(lines[0]), dict)


def test_orchestrator_skill_has_goal_message_routing_and_approval_workflow():
    from pathlib import Path
    import re

    import yaml

    path = Path(__file__).resolve().parents[2] / "skills/fa-orchestrator/SKILL.md"
    text = path.read_text()
    assert len(text.splitlines()) <= 150
    frontmatter = re.match(r"\A---\s*\n(.*?)\n---", text, re.S)
    assert frontmatter and yaml.safe_load(frontmatter[1])["name"] == "fa-orchestrator"
    normalized = " ".join(text.lower().split())
    for required in (
        "/goal", "all approved issues are merged or only items waiting for a human decision remain",
        "no blocked items", "fa gate status --json", "fa orch watch", "fa orch list",
        "fa orch escalate", "fa orch merge", "askuserquestion", "fa notify",
        "status question", "answer to a pending decision", "new work or change",
        "change to a running issue", "thought", "fa order publish --approve",
        "[add]", "[edit]", "[later]",
    ):
        assert required in normalized
