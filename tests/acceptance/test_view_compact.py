"""Acceptance test for view-compact. Locked by Foragent; do not edit."""

import pytest

from test_stall_visibility import _lane_rows


@pytest.mark.parametrize("language,idle_word", [("en", "idle"), ("ko", "쉬는 레인")])
def test_idle_lanes_fold_into_one_line_and_rows_stay_one_line(
    git_history, tmp_path, monkeypatch, capsys, language, idle_word,
):
    from test_view_status import status_case
    from test_view_done_state import cell_count
    from test_squirrel_running import plain_frame
    case,main=status_case(git_history,tmp_path,monkeypatch,language=language)
    title='A very long issue title that would never fit in the narrow column of the table'
    case.tracker.update_issue(4,title=title,body='')
    assert main(['gate','watch','--once','--repo-root',str(case.root)])==0
    frame=plain_frame(capsys.readouterr().out)
    lines=frame.splitlines()
    assert any('Docs' in line and ('Common' in line or '공통' in line) for line in lines), 'replacement: idle lanes share one sleeping block'
    for number in (1,2,3,4):
        assert f'#{number}' in frame
    assert any('▌' in line and '#1' in line for line in lines)
    assert title not in frame, 'long titles belong in click explanations, not wrapped acorns'
    assert all(cell_count(line)<=120 for line in lines)

