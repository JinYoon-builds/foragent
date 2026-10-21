"""Acceptance test for orch-skill. Locked by Foragent; do not edit."""


def test_watch_emits_for_blocked_human_merge_and_queue_empty_changes(git_history, tmp_path, monkeypatch, capsys):
    import json

    from test_orch_review_handoff import bind_cli, make_case

    case = make_case(git_history, tmp_path, monkeypatch)
    main = bind_cli(case, monkeypatch)

    def changed():
        assert main(["orch", "watch", "--interval", "0.01"]) == 0
        lines = capsys.readouterr().out.splitlines()
        assert len(lines) == 1
        assert isinstance(json.loads(lines[0]), dict)
        assert (case.store.root / "orch-watch.json").is_file()
        return lines[0]

    baseline = changed()
    case.tracker.add_labels(1, ["blocked"])
    blocked = changed()
    case.tracker.add_labels(1, ["needs-human"])
    human = changed()
    pr = case.tracker.create_pr(head="foragent/issue-1", base="main", title="Feature", body="Closes #1")
    case.tracker.merge_pr(pr, expected_head_sha=case.tracker.pr_head_sha(pr))
    merged = changed()
    case.store.finish_run(1, outcome="merged")
    case.tracker.close_issue(1)
    empty = changed()
    assert len({baseline, blocked, human, merged, empty}) == 5


def test_watch_waits_through_identical_polls_and_ignores_title_edits(git_history, tmp_path, monkeypatch, capsys):
    import json
    import time

    from test_orch_review_handoff import bind_cli, make_case

    case = make_case(git_history, tmp_path, monkeypatch)
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
        if len(polls) == 2:
            case.tracker.add_labels(1, ["blocked"])

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
