"""Acceptance test for lane-m2-bubble. Locked by Foragent; do not edit."""

from dataclasses import replace
from datetime import timedelta
import os
import time

import pytest

from test_acorn_interact import hit_positions
from test_local_truth import record_gh, snapshot_case
from test_progress_words import local_snapshot, progress_case
from test_skills_autosync import isolated_home
from test_squirrel_running import plain_frame, single_worker, utf8_terminal, view_offline
from test_view_done_state import cell_count



@pytest.fixture(autouse=True)
def utc_bubble_dates():
    previous = os.environ.get('TZ')
    os.environ['TZ'] = 'UTC'
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = previous
        time.tzset()


def bubble_lines(frame):
    lines = plain_frame(frame).splitlines()
    top = next((i for i, line in enumerate(lines) if "╭" in line and "╮" in line), None)
    assert top is not None, "r54: the orchestrator needs its full-height speech bubble even while working"
    assert top + 7 < len(lines) and "╰" in lines[top + 7] and "╯" in lines[top + 7], (
        "the bubble must be exactly eight rows: border, six body rows, border")
    return lines[top:top + 8]


@pytest.mark.parametrize("language,working,queued,question,hint,merges", [
    ("ko", "레인 1곳에서 이슈 1개를 작업하고 있어요.", "2개는 순서를 기다려요.",
     "답할 질문이 1개 있어요. 기획 세션에서 물어볼게요.", "도토리를 누르면 무슨 일인지 알려 줄게요.", "방금 #44, #43, #42, #41를 머지했어요."),
    ("en", "Working on 1 issue in 1 lane.", "2 wait their turn.",
     "1 question to answer; I will ask in the planning session.", "Click an acorn and I will explain it.",
     "Just merged #44, #43, #42, #41."),
])
def test_default_eight_row_bubble_has_summary_questions_latest_four_merges_and_hint(
    tmp_path, language, working, queued, question, hint, merges,
):
    from foragent.tracker import Issue
    from foragent.watch import render_frame

    snapshot, config = single_worker()
    config = replace(config, language=language)
    snapshot.status["lanes"]["work"].update(
        queued=[{"number": n, "title": f"Next {n}"} for n in (102, 103)],
        held=[{"number": 104, "title": "Choose a name", "state": "waiting-human"}])
    snapshot.status["issues"] = {"needs-human": [{"number": 104, "title": "Choose a name"}]}
    snapshot.status["summary"].update(queued=2, waiting_human=1)
    snapshot.done = {"work": [Issue(n, f"Merged {n}", "", frozenset(), "closed") for n in (40, 41, 42, 43, 44)]}
    snapshot.merged_at = {n: snapshot.captured_at - timedelta(minutes=45 - n) for n in range(40, 45)}
    frame = render_frame(snapshot, config, tmp_path, width=240)
    bubble = bubble_lines(frame)
    assert all(any(glyph in line for glyph in ("▀", "▄")) for line in bubble), (
        "the eight bubble rows must align with the big squirrel's eight art rows")
    words = "\n".join(bubble)
    for phrase in (working, queued, question, hint, merges):
        assert phrase in words, phrase
    assert "#40" not in words, "show the latest four merges, ordered by their times"
    assert "Jin" not in words


@pytest.mark.parametrize("language,label", [("ko", "80% · 이슈 10개 중 8개 완료"),
                                            ("en", "80% · 8 of 10 issues done")])
def test_r57_bar_uses_today_plus_unique_approved_remaining_and_ignores_old_order_counts(tmp_path, language, label):
    from foragent.watch import render_frame

    snapshot, config = single_worker()
    snapshot = replace(snapshot, completed=777, total=999, orders=27, today=8)
    snapshot.status["issues"] = {
        "running": [{"number": 101}], "ready": [{"number": 102}, {"number": 101}],
        "approved-waiting": [{"number": 102}], "needs-human": [], "pr-open": [],
        "unapproved": [{"number": 9999}],
    }
    frame = plain_frame(render_frame(snapshot, replace(config, language=language), tmp_path, width=160))
    bars = [line for line in frame.splitlines() if "░" in line or "████" in line]
    assert len(bars) == 1 and label in bars[0], "r57 total is today's 8 merges plus 2 distinct approved issues"
    assert all(word not in bars[0] for word in ("777", "999", "27", "3시간", "3 hours", "epic", "주문"))
    top = bubble_lines(frame)
    assert frame.splitlines().index(bars[0]) == frame.splitlines().index(top[-1]) + 1, (
        "the progress bar sits directly under the big squirrel and its bubble")


@pytest.mark.parametrize("group", ["running", "ready", "approved-waiting", "needs-human", "pr-open"])
def test_r57_each_approved_remaining_group_contributes_to_total(tmp_path, group):
    from foragent.watch import render_frame

    snapshot, config = single_worker()
    snapshot.today = 1
    snapshot.status["issues"] = {group: [{"number": 101}]}
    frame = plain_frame(render_frame(snapshot, config, tmp_path, width=160))
    assert "50% · 이슈 2개 중 1개 완료" in frame, f"r57 must count {group}"


def test_r57_uses_collect_facts_without_changing_existing_order_totals(git_history, tmp_path, monkeypatch, capsys):
    from foragent.watch import render_frame

    case = progress_case(git_history, tmp_path, monkeypatch)
    snapshot, calls = local_snapshot(case, monkeypatch, capsys)
    assert (snapshot.completed, snapshot.total, snapshot.today) == (12, 41, 14)
    frame = plain_frame(render_frame(snapshot, case.config, case.root, width=240))
    assert "33% · 이슈 43개 중 14개 완료" in frame, (
        "r57 display must use collect's today=14 and 29 remaining, not its historical 12/41 order total")
    low, high = snapshot.eta_range
    words = "\n".join(bubble_lines(frame))
    assert low.astimezone().strftime("%H:%M") in words and high.astimezone().strftime("%H:%M") in words
    assert "이대로면" in words and "쯤 다 끝나요." in words
    assert f"최근 3시간에 {snapshot.recent}개를 머지했어요." in words
    bar = next(line for line in frame.splitlines() if "33%" in line)
    assert "3시간" not in bar and calls == []


@pytest.mark.parametrize("language,idle", [
    ("ko", "지금은 할 일이 없어요. 새 일은 기획 세션에서 말해 주세요."),
    ("en", "Nothing to do now. Tell the planning session about new work."),
])
def test_no_work_has_r56_idle_sentence_and_no_invented_finish_time(tmp_path, language, idle):
    from test_view_done_state import done_snapshot
    from foragent.watch import render_frame

    snapshot, config = done_snapshot(count=1, language=language)
    snapshot.today = 0
    snapshot.status["issues"] = {}
    frame = render_frame(snapshot, config, tmp_path, width=240)
    words = "\n".join(bubble_lines(frame))
    assert idle in words
    assert all(word not in words for word in ("이대로면", "done around", "0/0", "100%"))


@pytest.mark.parametrize("language,hint", [
    ("ko", "도토리를 누르면 무슨 일인지 알려 줄게요."), ("en", "Click an acorn and I will explain it."),
])
def test_new_bubble_copy_is_read_through_the_existing_messages_registry(tmp_path, monkeypatch, language, hint):
    from foragent import i18n, watch

    keys = [key for key, template in i18n.MESSAGES[language].items() if template == hint]
    assert keys, "r56 hint must live in the discovered per-feature message catalogue"
    snapshot, config = single_worker()
    for key in keys:
        monkeypatch.setitem(i18n.MESSAGES[language], key, "MESSAGE_REGISTRY_PROBE")
    frame = watch.render_frame(snapshot, replace(config, language=language), tmp_path, width=240)
    assert "MESSAGE_REGISTRY_PROBE" in "\n".join(bubble_lines(frame)), "the visible hint must call text(), not duplicate the copy"


@pytest.mark.parametrize("language,phase,sentence,elapsed", [
    ("ko", "baseline", "완료 테스트가 지금 실패하는지 확인하고 있어요", "12분"),
    ("ko", "implementing", "코드를 쓰고 있어요", "12분"),
    ("ko", "checking", "내 컴퓨터에서 관련 테스트를 돌리고 있어요", "12분"),
    ("ko", "reviewing", "다른 AI가 리뷰하고 있어요", "12분"),
    ("ko", "ci", "GitHub에서 CI를 돌리고 있어요", "12분"),
    ("ko", "merging", "main에 머지하고 있어요", "12분"),
    ("en", "baseline", "checking that the acceptance tests fail now", "12m"),
    ("en", "implementing", "writing code", "12m"),
    ("en", "checking", "running related tests on this computer", "12m"),
    ("en", "reviewing", "another AI is reviewing", "12m"),
    ("en", "ci", "running CI on GitHub", "12m"),
    ("en", "merging", "merging into main", "12m"),
])
def test_acorn_click_explains_local_title_lane_phase_and_phase_time_in_bubble(
    git_history, tmp_path, monkeypatch, language, phase, sentence, elapsed,
):
    from foragent import status, watch

    case = snapshot_case(git_history, tmp_path, monkeypatch)
    config = replace(case.config, language=language)
    started = (case.clock[0] - timedelta(minutes=12)).isoformat()
    case.store.update_run(1, phase=phase, phase_started_at=started)
    snapshot = watch.collect(status.collect(case.store, config, case.clock[0]), case.tracker, case.store, config, case.clock[0])
    calls = record_gh(monkeypatch)
    state, layout = {"store": case.store}, {}
    before = watch.render_frame(snapshot, config, case.root, width=240, state=state, layout=layout)
    hits = hit_positions(layout, before, 240)
    assert ("acorn", 1) in hits
    x, y = hits[("acorn", 1)]
    watch.handle_mouse("left", x, y, layout, case.store, config, state=state)
    opened = watch.render_frame(snapshot, config, case.root, width=240, state=state, layout=layout)
    words = "\n".join(bubble_lines(opened))
    for expected in ("#1", "Implement feature", "api", sentence, elapsed):
        assert expected in words, expected
    assert ("acorn", 1) in hit_positions(layout, opened, 240), "explaining an issue must keep the lane blocks visible"
    assert calls == []


@pytest.mark.parametrize("close", ["esc", "blank-click"])
def test_escape_or_other_click_restores_default_bubble_and_keeps_lanes(git_history, tmp_path, monkeypatch, close):
    from foragent import status, watch

    case = snapshot_case(git_history, tmp_path, monkeypatch)
    snapshot = watch.collect(status.collect(case.store, case.config, case.clock[0]), case.tracker,
                             case.store, case.config, case.clock[0])
    state, layout = {"store": case.store}, {}
    before = watch.render_frame(snapshot, case.config, case.root, width=160, state=state, layout=layout)
    x, y = hit_positions(layout, before, 160)[("acorn", 1)]
    watch.handle_mouse("left", x, y, layout, case.store, case.config, state=state)
    opened = watch.render_frame(snapshot, case.config, case.root, width=160, state=state, layout=layout)
    assert "Implement feature" in "\n".join(bubble_lines(opened))
    if close == "esc":
        assert watch.handle_key("esc", case.store, case.config, state=state) is False, "Esc closes the explanation, not the view"
    else:
        blank = next((x, y) for y in range(len(opened.splitlines())) for x in range(160)
                     if watch.hit_test(layout, x, y) is None)
        watch.handle_mouse("left", *blank, layout, case.store, case.config, state=state)
    restored = watch.render_frame(snapshot, case.config, case.root, width=160, state=state)
    assert plain_frame(restored) == plain_frame(before), "closing restores the original summary frame"




def test_manual_snapshot_merges_still_obey_local_midnight_and_capture_time(tmp_path):
    from foragent.watch import render_frame
    snapshot, config = single_worker()
    midnight = snapshot.captured_at.astimezone().replace(hour=0,minute=0,second=0,microsecond=0)
    snapshot.merged_at = {41: midnight-timedelta(seconds=1), 42: midnight,
                          43: snapshot.captured_at, 44: snapshot.captured_at+timedelta(seconds=1)}
    words = '\n'.join(bubble_lines(render_frame(snapshot, config, tmp_path, width=240)))
    assert '#43, #42' in words and '#41' not in words and '#44' not in words


def test_long_selected_title_uses_one_row_and_retains_lane_phase_and_elapsed(tmp_path):
    from foragent.state import StateStore
    from test_lane_m2_contract import pick, bubble
    snapshot, config = single_worker(title='긴 제목 ' * 80 + 'HIDDEN_TAIL')
    opened, state = pick(snapshot, config, tmp_path, StateStore(tmp_path/'state'), ('acorn',101), width=100)
    words = bubble(opened)
    title_row = next(line for line in words.splitlines() if '#101' in line)
    assert '긴 제목' in title_row and '…' in title_row and 'HIDDEN_TAIL' not in words
    assert sum('긴 제목' in line for line in words.splitlines()) == 1
    assert all(part in words for part in ('work','코드를 쓰고 있어요','12분'))
    assert state['explanation_target'] == ('acorn',101) and state.get('issue') is None
