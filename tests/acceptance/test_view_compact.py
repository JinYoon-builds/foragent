"""Acceptance test for view-compact. Locked by Foragent; do not edit."""

import pytest


@pytest.mark.parametrize("language,idle_word", [("en", "idle"), ("ko", "쉬는 레인")])
def test_idle_lanes_fold_into_one_line_and_rows_stay_one_line(
    git_history, tmp_path, monkeypatch, capsys, language, idle_word,
):
    from test_view_status import status_case

    case, main = status_case(git_history, tmp_path, monkeypatch, language=language)
    case.tracker.add_issue(7, "A very long issue title that would never fit in the narrow column of the table",
                           labels=["approved", "ready", "lane:web"])
    assert main(["gate", "watch", "--once", "--repo-root", str(case.root)]) == 0
    lines = capsys.readouterr().out.splitlines()
    text = "\n".join(lines)

    # Lanes with work keep their full road rows; idle lanes (Docs, common) fold.
    for busy in ("Api", "Decision", "Review", "Web"):
        assert sum(f"{busy} " in line and "▐" in line for line in lines) == 1, busy
    assert not any("Docs" in line and "▐" in line for line in lines)
    folded = [line for line in lines if idle_word in line.lower()]
    assert len(folded) == 1
    assert "3 🧩 Docs" in folded[0] and "6 🧰" in folded[0]
    assert "1–6" in text  # every lane keeps its detail key

    # The work table lists only lanes with work, one line per lane, cut with an ellipsis.
    start = next(i for i, line in enumerate(lines) if line.startswith("┌") and "┐" in line and i > 3)
    end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("└"))
    rows = [line for line in lines[start + 2:end]]
    assert len(rows) == 4, rows
    assert all(line.startswith("│🧩") for line in rows), rows
    assert "…" in text
    assert "할 일 없음" not in text and "nothing to do" not in text.lower()
