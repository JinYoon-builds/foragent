"""Acceptance test for view-clone. Locked by Foragent; do not edit."""

import pytest

from test_skills_autosync import isolated_home


def _view_clone_case(git_history, tmp_path, monkeypatch, *, workers, title="orch"):
    from test_orch_review_handoff import bind_cli, make_case

    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], language="en",
                     lanes={"orch": {"paths": ["src/foragent/**"], "title": title, "emoji": "🧭"},
                            "docs": {"paths": ["docs/**"], "title": "docs", "emoji": "📝"}})
    case.tracker.add_labels(1, ["lane:orch"])
    case.store.update_run(1, lane="orch", phase="implementing")
    case.tracker.add_issue(2, "Second orchestration issue",
                           labels=["approved", "running" if workers == 2 else "ready", "lane:orch"])
    if workers == 2:
        case.store.start_run(2, branch="foragent/issue-2", worktree=case.store.worktree_path(2), lane="orch")
        case.store.update_run(2, phase="checking")
    return case, bind_cli(case, monkeypatch)


@pytest.mark.parametrize("title", ["orch", "Control"])
def test_view_clone_two_workers_append_multiplier_to_lane_display_name(
    git_history, tmp_path, monkeypatch, capsys, title,
):
    from foragent.labels import lane_display
    from foragent.scheduler import tick
    from foragent.watch import render_frame
    from test_orch_review_handoff import tick_context
    from test_view_evidence import view_evidence_snapshot

    case, main = _view_clone_case(git_history, tmp_path, monkeypatch, workers=2, title=title)
    tick(tick_context(case, start_runs=False))
    snapshot = view_evidence_snapshot(case, main, capsys)
    frame = render_frame(snapshot, case.config, case.root, width=160, now=case.clock[0])
    # Jin, 2026-10-25: one row per running worker, never one lane row with a multiplier.
    # (Name kept from the multiplier era so running baselines stay valid.)
    name = lane_display(case.config, "orch", "en")
    assert "×" not in frame, frame
    rows = frame.splitlines()
    first = [row for row in rows if f"{name} 1" in row]
    second = [row for row in rows if f"{name} 2" in row]
    assert len(first) == 1 and "#1" in first[0], f"Worker 1 needs its own row with its issue:\n{frame}"
    assert len(second) == 1 and "#2" in second[0], f"Worker 2 needs its own row with its issue:\n{frame}"
    assert "#2" not in first[0] and "#1" not in second[0], frame
    idle = [row for row in rows if lane_display(case.config, "docs", "en") in row]
    assert idle and not any("#" in row for row in idle), f"An idle lane keeps its own row:\n{frame}"


def test_view_clone_both_active_issue_numbers_are_visible(git_history, tmp_path, monkeypatch, capsys):
    from foragent.scheduler import tick
    from foragent.watch import render_frame
    from test_orch_review_handoff import tick_context
    from test_view_evidence import view_evidence_snapshot

    case, main = _view_clone_case(git_history, tmp_path, monkeypatch, workers=2)
    tick(tick_context(case, start_runs=False))
    snapshot = view_evidence_snapshot(case, main, capsys)
    assert {run["number"] for run in snapshot.status["active_runs"]} == {1, 2}
    frame = render_frame(snapshot, case.config, case.root, width=160, now=case.clock[0])
    assert "#1" in frame and "#2" in frame, frame


def test_view_clone_one_worker_with_queued_work_has_no_multiplier(git_history, tmp_path, monkeypatch, capsys):
    from foragent.labels import lane_display
    from foragent.scheduler import tick
    from foragent.watch import render_frame
    from test_orch_review_handoff import tick_context
    from test_view_evidence import view_evidence_snapshot

    case, main = _view_clone_case(git_history, tmp_path, monkeypatch, workers=1)
    tick(tick_context(case, start_runs=False))
    snapshot = view_evidence_snapshot(case, main, capsys)
    assert [run["number"] for run in snapshot.status["active_runs"]] == [1]
    frame = render_frame(snapshot, case.config, case.root, width=160, now=case.clock[0])
    name = lane_display(case.config, "orch", "en")
    assert name in frame and "#1" in frame
    assert "×" not in frame, frame
    assert f"{name} 1" not in frame, "A lane with one worker keeps its plain name"
