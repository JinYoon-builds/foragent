"""Shared snapshot and measurement helpers for the n75 one-shot dashboard width tests."""

import os
import re
from datetime import timedelta

import pytest

from helpers.cells import cells as east_asian_cells

WIDTHS = (40, 80)
VS16 = "️"
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# Half-block pixel glyphs the squirrel sprite is drawn with (watch_art.pixel_rows).
PIXELS = set("▀▄█")

TITLES = {
    "ko": "대시보드 한 번 출력이 창 너비에 맞아야 하는 긴 이슈 제목입니다",
    "en": "A one shot dashboard frame must fit the width of its pane",
}
LANE_TITLES = {"ko": "화면 레인", "en": "View lane"}
QUESTION = {"ko": "사람의 답을 기다리는 질문 이슈입니다", "en": "An issue that waits for a person's answer"}


def terminal_cells(value: str) -> int:
    """Cells a terminal draws: East Asian Width, two for VS16 emoji and the squirrel."""
    total = 0
    for index, char in enumerate(value):
        if char == VS16:
            continue
        width = 2 if char == "🐿" else east_asian_cells(char)
        if index + 1 < len(value) and value[index + 1] == VS16 and width == 1:
            width = 2
        total += width
    return total


def _plain(text: str) -> str:
    return ANSI.sub("", text)


def _populated(git_history, tmp_path, monkeypatch, language):
    """Lanes with Korean or English titles, a running issue, queued work and a question."""
    from foragent.scheduler import tick
    from test_orch_review_handoff import bind_cli, make_case, tick_context

    lanes = {
        "view": {"title": LANE_TITLES[language], "emoji": "\U0001f6e0" + VS16, "paths": ["src/view/**"]},
        "docs": {"title": "문서" if language == "ko" else "Docs", "emoji": "🧩", "paths": ["docs/**"]},
    }
    case = make_case(git_history, tmp_path, monkeypatch, lanes=lanes, language=language)
    main = bind_cli(case, monkeypatch)
    case.tracker.add_issue(2, TITLES[language], labels=["approved", "lane:view", "running"])
    case.store.start_run(2, branch="foragent/2", worktree=tmp_path / "wt2", lane="view")
    case.store.update_run(2, lane="view", phase="implementing", pid=os.getpid(),
                          phase_started_at=(case.clock[0] - timedelta(seconds=150)).isoformat(),
                          last_activity_at=(case.clock[0] - timedelta(seconds=40)).isoformat())
    for number in (3, 4):
        case.tracker.add_issue(number, f"{TITLES[language]} {number}", labels=["approved", "lane:docs"])
    case.tracker.add_issue(5, QUESTION[language], labels=["approved", "needs-human", "lane:docs"])
    tick(tick_context(case, start_runs=False))
    return case, main


def _once(main, case, capsys, width):
    """`fa dashboard --once` in a pane `width` columns wide: (exit code, plain rows)."""
    capsys.readouterr()
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("COLUMNS", str(width))
        patch.setenv("LINES", "60")
        code = main(["dashboard", "--once", "--repo-root", str(case.root)])
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err, captured.err
    return code, _plain(captured.out).rstrip("\n").splitlines()


