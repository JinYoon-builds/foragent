"""Acceptance test for progress-words. Locked by Foragent; do not edit."""

from dataclasses import replace
from datetime import timedelta
import json
import re

from test_local_truth import local_cli, record_gh
from test_orch_review_handoff import make_case, tick_context
from test_skills_autosync import isolated_home
from test_squirrel_running import plain_frame, view_offline


def progress_case(git_history, tmp_path, monkeypatch, *, recent=3):
    """Three open orders, plus a closed order that must not inflate the totals."""
    from foragent.scheduler import tick

    case = make_case(git_history, tmp_path, monkeypatch, language="ko", skill_libraries=[],
                     lanes={"api": {"title": "Api", "paths": ["api/**"]}}, test_command="true")
    case.store.finish_run(1, outcome="cancelled")
    case.tracker.close_issue(1)
    now = case.clock[0]
    finished = 0
    for parent, total, completed, state in ((900, 20, 7, "open"), (901, 8, 0, "open"),
                                            (902, 13, 5, "open"), (903, 2, 2, "closed")):
        case.tracker.add_issue(parent, f"Order {parent}", state=state)
        for index in range(total):
            number = parent * 100 + index
            closed = index < completed
            case.tracker.add_issue(number, f"Local work {number}",
                                   labels=["lane:api"] if closed else ["approved", "ready", "lane:api"],
                                   state="closed" if closed else "open")
            case.tracker.set_parent(number, parent)
            if closed:
                # Only the first three merges are recent; issue totals are not a rate.
                age = timedelta(minutes=10 + finished * 10) if state == "open" and finished < recent else timedelta(hours=2)
                case.clock[0] = now - age - timedelta(minutes=5)
                case.store.start_run(number, branch=f"foragent/issue-{number}",
                                     worktree=case.store.worktree_path(number), lane="api")
                case.clock[0] = now - age
                case.store.update_run(number, last_progress_at=case.clock[0].isoformat())
                case.store.finish_run(number, outcome="merged")
                finished += 1
    case.clock[0] = now
    tick(tick_context(case, start_runs=False))
    return case


def local_snapshot(case, monkeypatch, capsys):
    """Observe with the real GhTracker, guarded at the same boundary as local-view."""
    from foragent.tracker import GhTracker
    from foragent.watch import collect

    main = local_cli(case, monkeypatch)
    calls = record_gh(monkeypatch)
    capsys.readouterr()
    assert main(["gate", "status", "--json", "--repo-root", str(case.root)]) == 0
    status = json.loads(capsys.readouterr().out)
    snapshot = collect(status, GhTracker("owner/repo"), case.store, case.ctx.config, case.clock[0])
    assert calls == [], "progress is a local observation"
    return snapshot, calls


def test_progress_words_all_open_orders_share_one_bar_and_totals_do_not_follow_first_worker(
    git_history, tmp_path, monkeypatch, capsys,
):
    from foragent.watch import render_frame

    case = progress_case(git_history, tmp_path, monkeypatch)
    snapshot, calls = local_snapshot(case, monkeypatch, capsys)
    assert (snapshot.completed, snapshot.total) == (12, 41)
    for first in (90007, 90100, 90205):
        snapshot.status["active_runs"] = [{"number": first, "lane": "api", "attempts": 1}]
        frame = plain_frame(render_frame(snapshot, case.config, case.root, width=100))
        assert frame.count("12/41") == 1
        bars = [line for line in frame.splitlines() if re.search(r"[█▓░]{2,}", line)]
        assert len(bars) == 1, bars
        assert all(value not in frame for value in ("7/20", "0/8", "5/13", "14/43"))
    assert calls == []


def test_progress_words_eta_is_clock_time_from_three_recent_merges_for_all_remaining_work(
    git_history, tmp_path, monkeypatch, capsys,
):
    from foragent.watch import render_frame

    case = progress_case(git_history, tmp_path, monkeypatch)
    snapshot, calls = local_snapshot(case, monkeypatch, capsys)
    finish = case.clock[0].astimezone() + timedelta(hours=29 / 3)
    expected = f"{finish.hour}시 {finish.minute:02d}분쯤 끝나요"
    frame = plain_frame(render_frame(snapshot, case.config, case.root, width=100, now=case.clock[0]))
    assert expected in frame, (expected, frame)
    assert "최근 1시간에 3개 끝냈어요" in frame
    assert calls == []


def test_progress_words_no_recent_merges_does_not_invent_an_end_time(
    git_history, tmp_path, monkeypatch, capsys,
):
    from foragent.watch import render_frame

    case = progress_case(git_history, tmp_path, monkeypatch, recent=0)
    snapshot, calls = local_snapshot(case, monkeypatch, capsys)
    frame = plain_frame(render_frame(snapshot, case.config, case.root, width=100))
    assert "끝나는 시각은 아직 몰라요" in frame
    assert not re.search(r"\d+시\s+\d+분쯤 끝나요|\d{1,2}:\d{2}.*끝나요", frame)
    assert "12/41" in frame and calls == []


def test_progress_words_recorded_quota_includes_percentage_reset_and_words(
    git_history, tmp_path, monkeypatch, capsys,
):
    from datetime import UTC, datetime
    from foragent.watch import render_frame
    from test_usage_read import CODEX_RESET, usage_case

    case = usage_case(git_history, tmp_path, monkeypatch)
    case.ctx.config = replace(case.config, language="ko")
    # The runner is fake; observation still uses the real, guarded gh boundary.
    snapshot, calls = local_snapshot(case, monkeypatch, capsys)
    frame = plain_frame(render_frame(snapshot, case.ctx.config, case.root, width=160))
    quota = next((line for line in frame.splitlines() if "쿼터" in line and "Codex 67%" in line), "")
    assert quota, frame
    assert "리셋" in quota
    assert datetime.fromtimestamp(CODEX_RESET, UTC).astimezone().strftime("%H:%M") in frame
    assert calls == []
