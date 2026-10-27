"""Acceptance test for view-waiting-phases. Locked by Foragent; do not edit."""

from datetime import timedelta

import pytest

from foragent.i18n import MESSAGES, text
from test_orch_review_handoff import tick_context
from test_skills_autosync import isolated_home
from test_stall_visibility import _api_row
from test_view_status import status_case


def _run_row(git_history, tmp_path, monkeypatch, *, phase, runner_minutes,
             language="en", phase_minutes=12):
    from foragent.scheduler import tick
    from foragent.status import collect as collect_status
    from foragent.watch import collect, render_frame

    case, _ = status_case(git_history, tmp_path, monkeypatch, language=language)
    # Refresh local issues before installing the silence under test, so the
    # scheduler cannot recover the run before the view gets to inspect it.
    tick(tick_context(case, start_runs=False))
    now = case.clock[0]
    case.store.update_run(
        1, pid=88001, phase=phase,
        started_at=(now - timedelta(minutes=30)).isoformat(),
        phase_started_at=(now - timedelta(minutes=phase_minutes)).isoformat(),
        last_activity_at=(now - timedelta(minutes=20)).isoformat(),
        last_progress_at=(now - timedelta(minutes=runner_minutes)).isoformat(),
    )
    monkeypatch.setattr("foragent.watch.os.kill", lambda pid, signal: None)
    status = collect_status(case.store, case.config, now)
    snapshot = collect(status, case.tracker, case.store, case.config, now)
    return _api_row(render_frame(snapshot, case.config, case.root, width=90, now=now))


@pytest.mark.parametrize("phase,language,phase_minutes,age", [
    pytest.param("checking", "en", 12, "12 min", id="checking-en"),
    pytest.param("ci", "en", 5, "5 min", id="ci-en"),
    pytest.param("checking", "ko", 12, "12분", id="checking-ko"),
])
def test_waiting_with_fresh_runner_shows_localized_phase_and_age(
    git_history, tmp_path, monkeypatch, phase, language, phase_minutes, age,
):
    row = _run_row(git_history, tmp_path, monkeypatch, phase=phase,
                   runner_minutes=1, language=language, phase_minutes=phase_minutes)
    stalled = MESSAGES[language]["watch_stalled"].partition("{minutes}")[0].strip()
    missing = [label for label in (text(phase, language), age) if label not in row]
    assert not missing and stalled not in row, (
        f"Fresh runner in {phase}: missing phase/age {missing!r}; "
        f"stalled={stalled in row}; row={row}"
    )


@pytest.mark.parametrize("phase", ["checking", "implementing"])
def test_both_worker_and_runner_silent_past_threshold_show_stalled(
    git_history, tmp_path, monkeypatch, phase,
):
    row = _run_row(git_history, tmp_path, monkeypatch, phase=phase, runner_minutes=20)
    assert text("watch_stalled", "en", minutes=20) in row, row


def test_fresh_runner_progress_also_keeps_implementing_from_looking_stalled(
    git_history, tmp_path, monkeypatch,
):
    row = _run_row(git_history, tmp_path, monkeypatch, phase="implementing", runner_minutes=1)
    stalled = MESSAGES["en"]["watch_stalled"].partition("{minutes}")[0].strip()
    assert stalled not in row, f"Fresh runner progress must count during implementation: {row}"
