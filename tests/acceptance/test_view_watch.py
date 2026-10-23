"""Acceptance test for view-watch. Locked by Foragent; do not edit."""

import pytest


@pytest.mark.parametrize("language,words", [
    ("en", ("orchestrator", "now doing", "looks stalled", "handled by the orchestrator")),
    ("ko", ("오케스트레이터", "지금 하는 일", "멈춘 듯", "처리한 일")),
])
def test_once_frame_contains_lane_queue_done_stall_and_recent_events(
    git_history, tmp_path, monkeypatch, capsys, language, words,
):
    import json

    from foragent.scheduler import tick
    from test_orch_review_handoff import tick_context
    from test_view_status import status_case

    case, main = status_case(git_history, tmp_path, monkeypatch, language=language)
    case.tracker.add_issue(5, "Next API task", labels=["approved", "ready", "lane:api"])
    for number in (10, 20):
        case.tracker.add_issue(number, f"Finished {number}", labels=["lane:api"], state="closed")
        pr = case.tracker.create_pr(head=f"foragent/issue-{number}", base="main", title=f"Finished {number}",
                                    body=f"Closes #{number}")
        case.tracker.merge_pr(pr, expected_head_sha=case.tracker.pr_head_sha(pr))
        case.store.start_run(number, branch=f"foragent/issue-{number}",
                             worktree=case.store.worktree_path(number), lane="api")
        case.store.finish_run(number, outcome="merged")
    case.tracker.add_issue(99, "Current order")
    for number in (1, 5, 10, 20):
        case.tracker.set_parent(number, 99)
    tick(tick_context(case, start_runs=False))
    events = [{"at": case.clock[0].isoformat(), "issue": 2, "kind": "retry", "summary": f"handled-event-{i}"}
              for i in range(7)]
    (case.store.root / "orch-events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    assert main(["gate", "watch", "--once", "--repo-root", str(case.root)]) == 0
    output = capsys.readouterr().out
    assert "🧩 Api" in output and "#5" in output and "✓ 2" in output
    assert "○" in output and "11" in output
    assert "handled-event-6" in output and "handled-event-0" not in output and "handled-event-1" not in output
    assert "2/4" in output and "1" in output
    assert "✳" in output or "✺" in output
    assert all(word in output.lower() for word in words)
    assert "\x1b[" not in output


def test_agent_detail_render_exposes_config_and_only_first_three_memory_lines(tmp_path):
    from foragent.config import Config
    from foragent.watch import render_detail

    config = Config(language="en", lanes={"api": {
        "paths": ["api/**"], "agent": "fa-api", "title": "Server", "emoji": "🔧",
        "executor": "claude", "model": "test-model", "skills": ["testing-strategy"],
    }})
    memory = tmp_path / ".claude/agent-memory/fa-api/MEMORY.md"
    memory.parent.mkdir(parents=True)
    memory.write_text("First lesson\nSecond lesson\nThird lesson\nFourth must stay hidden\n")
    text = render_detail(config, "api", tmp_path, done_count=2)
    assert isinstance(text, str)
    assert all(value in text for value in ("Server", "api/**", "claude", "test-model", "testing-strategy",
                                           "2", "First lesson", "Second lesson", "Third lesson"))
    assert "Fourth must stay hidden" not in text
