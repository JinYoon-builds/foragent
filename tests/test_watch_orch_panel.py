from datetime import UTC, datetime, timedelta

from foragent import orch_inbox
from foragent.state import StateStore
from foragent.watch_sections import orch_panel

NOW = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)


def plain(lines):
    return ["".join(value for value, style in line) for line in lines]


def cells(value):
    from foragent import watch

    return sum(watch._cells(char) for char in value)


def test_age_text_tiers_in_both_languages():
    cases = [(30, "방금", "just now"), (180, "3분 전", "3 min ago"),
             (2 * 3600 + 59, "2시간 전", "2 h ago"), (3 * 86400, "3일 전", "3 d ago")]
    for seconds, ko, en in cases:
        assert orch_panel.age_text(seconds, "ko") == ko
        assert orch_panel.age_text(seconds, "en") == en


def test_where_text_shows_only_the_value():
    assert orch_panel.where_text({"kind": "orca", "title": "orch: foragent"}, "ko") == "Orca 탭 orch: foragent"
    assert orch_panel.where_text({"kind": "orca", "handle": "h", "title": None}, "en") == "Orca tab"
    assert orch_panel.where_text({"kind": "tmux", "pane": "%7"}, "en") == "tmux pane %7"
    assert orch_panel.where_text({"kind": "terminal"}, "ko") == "터미널"
    assert orch_panel.where_text(None, "ko") is None


def test_lines_order_hidden_empty_labels_age_and_stale():
    note = {"now": "CI를 봐요", "why": "", "next": None, "where": {"kind": "terminal"}}
    panel = {"note": note, "note_age": 170.0, "inbox": 4}
    shown = plain(orch_panel.lines(panel, 1, "ko", 34, 10.0, fresh=False))
    assert shown == ["지금: CI를 봐요 · 3분 전 (오래됨)", "터미널", "사람이 볼 것 4"]
    fresh = plain(orch_panel.lines({**panel, "note": {**note, "why": "검사", "next": "머지"}}, 1, "en", 34, 0.0, True))
    assert fresh == ["now: CI를 봐요 · 2 min ago", "why: 검사", "next: 머지", "terminal", "For you 4"]


def test_without_note_only_the_count_line_falls_back_to_the_summary():
    assert plain(orch_panel.lines({}, 2, "ko", 26, 0.0, True)) == ["사람이 볼 것 2"]
    assert plain(orch_panel.lines({}, None, "en", 26, 0.0, True)) == ["For you ?"]


def test_long_values_wrap_under_the_value_within_the_box():
    note = {"now": "막힌 이슈를 보고 있어요 그리고 설명을 다시 읽어요", "why": None, "next": None, "where": None}
    shown = plain(orch_panel.lines({"note": note, "note_age": None}, 0, "ko", 26, 0.0, True))
    now_lines = shown[:-1]
    assert len(now_lines) > 1 and now_lines[0].startswith("지금: ")
    assert all(line.startswith(" " * cells("지금: ")) for line in now_lines[1:])
    assert all(cells(line) <= 26 for line in shown)
    assert "".join(line.strip() for line in now_lines).replace(" ", "").endswith("다시읽어요")


def test_collect_uses_a_fresh_inspect_count_only(tmp_path):
    store = StateStore(tmp_path / "state")
    orch_panel.write_count(store, 5, NOW)
    assert not (tmp_path / "state").exists(), "inspect must not create the state folder"
    store.root.mkdir(parents=True)
    orch_panel.write_count(store, 5, NOW - timedelta(minutes=9))
    assert orch_panel.collect(store, NOW)["inbox"] == 5
    orch_panel.write_count(store, 5, NOW - timedelta(minutes=11))
    assert orch_panel.collect(store, NOW)["inbox"] is None
    (store.root / orch_panel.COUNT_FILE).write_text("not json", encoding="utf-8")
    assert orch_panel.collect(store, NOW) == {"note": None, "note_age": None, "inbox": None}


def test_note_location_keeps_an_explicit_orca_title_without_a_handle():
    assert orch_inbox.note_location({}, "orch: p") == {"kind": "orca", "handle": None, "title": "orch: p"}
    assert orch_inbox.note_location({"TMUX": "x", "TMUX_PANE": "%2"}, "orch: p")["kind"] == "orca"
    assert orch_inbox.note_location({}, "orch: p", "%7") == {"kind": "tmux", "pane": "%7"}
    assert orch_inbox.note_location({"FORAGENT_ORCH_TITLE": "t"}) == {"kind": "terminal"}


def test_box_stands_beside_the_sections_only_on_screens_of_140_cells_or_more():
    # Orchestrator decision 2026-10-27 (Q1=A): 140+ cells get a fixed 36-cell box; narrower stacks it.
    assert orch_panel.SIDE == 140
    assert orch_panel.box_width(99) == 60 and orch_panel.box_width(139) == 60
    assert orch_panel.box_width(140) == 36 and orch_panel.box_width(160) == 36
    assert orch_panel.box_width(40) == 40
