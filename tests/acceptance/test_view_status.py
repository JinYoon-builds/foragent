"""Acceptance test for view-status. Locked by Foragent; do not edit."""

import pytest


def status_case(git_history, tmp_path, monkeypatch, *, language="en"):
    from datetime import timedelta

    from foragent.tracker import Dependency
    from test_orch_review_handoff import bind_cli, make_case

    lanes = {slug: {"paths": [f"{slug}/**"], "title": slug.title(), "emoji": "🧩"}
             for slug in ("api", "review", "decision", "web", "docs")}
    case = make_case(git_history, tmp_path, monkeypatch, lanes=lanes, language=language)
    case.tracker.add_labels(1, ["lane:api"])
    case.store.update_run(1, lane="api", phase="reviewing",
                          phase_started_at=(case.clock[0] - timedelta(seconds=120)).isoformat(),
                          last_activity_at=(case.clock[0] - timedelta(seconds=660)).isoformat())
    case.tracker.add_issue(2, "Review dispute", labels=["approved", "blocked", "lane:review"])
    case.tracker.add_issue(3, "Choose locale", labels=["approved", "needs-human", "lane:decision"])
    case.tracker.add_issue(4, "Login page", labels=["approved", "lane:web"])
    case.tracker.set_blocked_by(4, [Dependency(1, "open")])
    return case, bind_cli(case, monkeypatch)


def test_runner_records_phases_in_order_and_executor_activity(git_history, tmp_path, monkeypatch):
    from datetime import timedelta

    from foragent.runner import run_issue
    from test_orch_review_handoff import make_case

    case = make_case(git_history, tmp_path, monkeypatch)
    observed = []
    update = case.store.update_run

    def record(number, **fields):
        run = update(number, **fields)
        if "phase" in fields:
            observed.append((run.phase, run.phase_started_at))
        return run

    monkeypatch.setattr(case.store, "update_run", record)

    def implement(kwargs):
        case.implement(kwargs)
        case.clock[0] += timedelta(seconds=7)
        kwargs["log_path"].parent.mkdir(parents=True, exist_ok=True)
        with kwargs["log_path"].open("a") as log:
            log.write("executor made progress\n")

    def poll(execute, progress):
        result = execute()
        progress()
        assert case.store.get_run(1).last_activity_at == case.clock[0].isoformat()
        return result

    case.implementer.steps = [implement]
    case.ctx.poll_hook = poll
    assert run_issue(1, case.ctx) == "merged"
    phases = [phase for index, (phase, at) in enumerate(observed)
              if index == 0 or phase != observed[index - 1][0]]
    assert phases == ["implementing", "checking", "reviewing", "ci", "merging"]
    assert all(at for phase, at in observed)


def test_status_json_covers_all_lane_states_and_blocker_numbers(git_history, tmp_path, monkeypatch, capsys):
    import json

    from foragent.scheduler import tick
    from test_orch_review_handoff import tick_context

    case, main = status_case(git_history, tmp_path, monkeypatch)
    tick(tick_context(case, start_runs=False))
    assert main(["gate", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    rows = payload["lanes"]
    lanes = {row["slug"]: row for row in (rows.values() if isinstance(rows, dict) else rows)}
    assert payload["summary"] == {"working": 1, "blocked": 1, "waiting_human": 1, "queued": 1}
    assert {slug: lanes[slug]["state"] for slug in ("api", "review", "decision", "web", "docs")} == {
        "api": "working", "review": "blocked", "decision": "waiting-human", "web": "queued", "docs": "idle"}
    assert lanes["api"]["issue"] == 1 and lanes["api"]["title"] == "Implement feature"
    assert lanes["api"]["phase"] == "reviewing"
    assert lanes["api"]["elapsed_seconds"] == 120
    assert lanes["api"]["last_activity_seconds"] == 660
    assert lanes["web"]["waiting_for"] == [1]
    assert lanes["docs"]["issue"] is None and lanes["docs"]["waiting_for"] == []


@pytest.mark.parametrize("language,words", [("en", ("working 1", "waiting for you 1", "queued 1")),
                                             ("ko", ("1",))])
def test_status_first_line_is_localized(git_history, tmp_path, monkeypatch, capsys, language, words):
    from foragent.scheduler import tick
    from test_orch_review_handoff import tick_context

    case, main = status_case(git_history, tmp_path, monkeypatch, language=language)
    tick(tick_context(case, start_runs=False))
    assert main(["gate", "status"]) == 0
    first = capsys.readouterr().out.splitlines()[0]
    assert all(word in first.lower() for word in words)
    assert "·" in first
    if language == "ko":
        import re

        assert re.search("[가-힣]", first)
        assert "waiting for you" not in first.lower()
