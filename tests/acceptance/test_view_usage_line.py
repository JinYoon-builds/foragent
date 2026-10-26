"""Acceptance test for view-usage-line. Locked by Foragent; do not edit."""

from datetime import timedelta
import json
import re

import pytest

from test_skills_autosync import isolated_home


def usage_snapshot(case, monkeypatch, capsys):
    from test_orch_review_handoff import bind_cli
    from foragent.watch import collect

    main = bind_cli(case, monkeypatch)
    capsys.readouterr()
    assert main(["gate", "status", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    return collect(status, case.tracker, case.store, case.ctx.config, case.clock[0]), main


@pytest.mark.parametrize("language,age", [("ko", "10분 전"), ("en", "10 min ago")])
def test_view_usage_line_last_line_contains_both_usage_ages_resets_capacity_and_mode(
    git_history, tmp_path, monkeypatch, capsys, language, age,
):
    from dataclasses import replace
    from datetime import UTC, datetime
    from foragent.watch import render_frame
    from test_usage_read import CLAUDE_RESET, CODEX_RESET, usage_case

    case = usage_case(git_history, tmp_path, monkeypatch, used_percent=6)
    # Feed Codex's real-shaped event through the review executor in the same run;
    # usage_case records both executor outputs for this display contract.
    case.ctx.config = replace(case.ctx.config, language=language, max_parallel=3)
    case.clock[0] += timedelta(minutes=10)
    snapshot, _ = usage_snapshot(case, monkeypatch, capsys)
    line = render_frame(snapshot, case.ctx.config, case.root, width=240, now=case.clock[0]).splitlines()[-1]
    assert "Claude" in line and "Codex" in line
    assert "6%" in line and "67%" in line
    assert age in line
    for reset in (CLAUDE_RESET, CODEX_RESET):
        assert datetime.fromtimestamp(reset, UTC).astimezone().strftime("%H:%M") in line
    assert "keep" in line
    assert re.search(r"(?<!\d)3(?!\d)", line), "explicit concurrency ceiling must be visible"


def test_view_usage_line_m_key_toggles_persisted_mode_both_ways(git_history, tmp_path, monkeypatch, capsys):
    from foragent.watch import handle_key
    from test_orch_review_handoff import bind_cli, make_case

    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[])
    main = bind_cli(case, monkeypatch)
    for expected in ("burn", "keep"):
        handle_key("m", case.store, case.ctx.config)
        capsys.readouterr()
        assert main(["quota", "mode", "--repo-root", str(case.root)]) == 0
        assert expected in capsys.readouterr().out.lower()


def test_view_usage_line_idle_with_remaining_quota_invites_next_order(git_history, tmp_path, monkeypatch, capsys):
    from dataclasses import replace
    from foragent.watch import render_frame
    from test_usage_read import usage_case

    case = usage_case(git_history, tmp_path, monkeypatch, used_percent=6)
    case.ctx.config = replace(case.ctx.config, language="ko")
    snapshot, _ = usage_snapshot(case, monkeypatch, capsys)
    assert all(row["state"] == "idle" for row in snapshot.status["lanes"].values())
    frame = render_frame(snapshot, case.ctx.config, case.root)
    assert "쿼터가 남아 있어요" in frame and "/fa" in frame


def test_view_usage_line_without_usage_still_renders_placeholder(tmp_path):
    from foragent.watch import render_frame
    from test_view_done_state import done_snapshot

    snapshot, config = done_snapshot()
    last = render_frame(snapshot, config, tmp_path, width=240).splitlines()[-1]
    assert "—" in last
    assert "Claude" in last and "Codex" in last
