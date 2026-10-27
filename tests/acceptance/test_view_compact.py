"""Acceptance test for view-compact. Locked by Foragent; do not edit."""

import pytest

from test_stall_visibility import _lane_rows


@pytest.mark.parametrize("language,idle_word", [("en", "idle"), ("ko", "쉬는 레인")])
def test_idle_lanes_fold_into_one_line_and_rows_stay_one_line(
    git_history, tmp_path, monkeypatch, capsys, language, idle_word,
):
    from test_view_status import status_case

    case, main = status_case(git_history, tmp_path, monkeypatch, language=language)
    # Only the first queued issue is visible, so give that issue the long title.
    case.tracker.update_issue(4, title="A very long issue title that would never fit in the narrow column of the table",
                              body="")
    assert main(["gate", "watch", "--once", "--repo-root", str(case.root)]) == 0
    lines = capsys.readouterr().out.splitlines()
    text = "\n".join(lines)

    # Each busy lane has one state-led issue row; idle lanes keep one short row each.
    lane_rows = _lane_rows(text)
    rows = []
    for lane, number in (("Api", 1), ("Decision", 3), ("Review", 2), ("Web", 4)):
        found = [line for line in lane_rows if f"🧩 {lane}" in line and f"#{number}" in line.split()]
        assert len(found) == 1, lane_rows
        rows.extend(found)
    idle = [line for line in lane_rows if line.lstrip().startswith(("💤", "[z]"))]
    assert len(idle) == 2, f"{idle_word}: {idle}"
    assert any("🧩 Docs" in line for line in idle)
    assert any("🧰" in line for line in idle)
    assert len(lane_rows) == len(rows) + len(idle) == 6
    assert "1–6" in text  # every lane keeps its detail key

    # The long title is cut on its lane row without adding a continuation row.
    assert "…" in rows[-1], rows[-1]
    assert "할 일 없음" not in text and "nothing to do" not in text.lower()
