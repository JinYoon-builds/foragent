from dataclasses import replace
from types import SimpleNamespace

from foragent.config import Config
from foragent.order import progress as record
from foragent.state import StateStore
from foragent.watch_sections import prep


def plain(lines):
    return ["".join(part for part, _ in line).rstrip() for line in lines]


def frame(facts, *, language="ko", glyphs="emoji", titles=None, width=100):
    snapshot = SimpleNamespace(prep=facts, titles=titles or {})
    config = replace(Config(), language=language, view_glyphs=glyphs)
    return plain(prep.render(snapshot, config, None, width=width))


def test_no_preparation_adds_no_rows():
    assert frame(None) == []


def test_ledger_scene_lists_decisions_and_asks_for_confirmation(tmp_path):
    store = StateStore(tmp_path)
    record(store, "start", title="Login")
    record(store, "ledger", add="D18 decisions live in the docs")
    lines = frame(store.order_progress(), language="en")
    assert lines[0] == "📝 Login · sorting the decisions"
    assert lines[1:] == ["D18 decisions live in the docs", "Please confirm these decisions."]


def test_chunk_table_marks_filled_empty_and_asked_cells_in_ascii(tmp_path):
    store = StateStore(tmp_path)
    record(store, "start", title="Login")
    record(store, "chunk", add="Auth")
    record(store, "cell", chunk="Auth", slot="goal", filled=True)
    record(store, "cell", chunk="Auth", slot="skills", ask="Which skill?")
    lines = frame(store.order_progress(), language="en", glyphs="ascii")
    assert lines[0] == "[+] Login · filling the blanks"
    header, row = lines[1], lines[2]
    assert row[header.index("Goal")] == "v"
    assert row[header.index("Lane")] == "."
    assert row[header.index("Skills")] == "?"
    assert lines[3:] == ["4 empty cells left", "Auth · Skills: Which skill?"]


def test_numbers_appear_only_after_publish_with_local_titles(tmp_path):
    store = StateStore(tmp_path)
    record(store, "start", title="Login")
    record(store, "tests", done=2, total=5)
    assert frame(store.order_progress(), language="en")[0] == "📝 Login · writing tests · tests 2/5"
    record(store, "publish", issue=42)
    row = frame(store.order_progress(), language="en", titles={42: "Sign in"})[0]
    assert row == "📝 Login · published · tests 2/5 · #42 Sign in"


def test_title_row_is_cut_to_the_width_and_never_wraps(tmp_path):
    store = StateStore(tmp_path)
    record(store, "start", title="A very long order title " * 5)
    lines = frame(store.order_progress(), language="en", width=30)
    assert lines == ["📝 A ve… · preparing the order"]  # 30 cells: the emoji takes two.
    tiny = frame(store.order_progress(), language="en", width=12)
    assert len(tiny) == 1 and tiny[0].endswith("…")


def test_long_title_gives_way_so_count_and_published_numbers_stay(tmp_path):
    store = StateStore(tmp_path)
    record(store, "start", title="T" * 90)
    record(store, "tests", done=3, total=20)
    row = frame(store.order_progress(), language="en")[0]
    assert row.endswith("· writing tests · tests 3/20") and "…" in row
    record(store, "publish", issue=151)
    record(store, "publish", issue=152)
    row = frame(store.order_progress(), language="en", titles={151: "A" * 40, 152: "B" * 40})[0]
    assert row.endswith("· published · tests 3/20 · #151 · #152") and "…" in row
    assert len(row) <= 100


def test_zero_test_count_is_a_recorded_fact_from_the_tests_step_on(tmp_path):
    store = StateStore(tmp_path)
    record(store, "start", title="Login")
    assert "0/0" not in frame(store.order_progress(), language="en")[0]
    record(store, "tests", done=0, total=0)
    for step in ("tests", "check", "approval"):
        if step != "tests":
            record(store, step)
        assert frame(store.order_progress(), language="en")[0].endswith("· tests 0/0")
    record(store, "publish", issue=7)
    assert "tests 0/0 · #7" in frame(store.order_progress(), language="en")[0]


def test_narrow_table_keeps_every_slot_column_and_the_asked_mark(tmp_path):
    store = StateStore(tmp_path)
    record(store, "start", title="Login")
    record(store, "chunk", add="A long chunk name here")
    record(store, "cell", chunk="A long chunk name here", slot="skills", ask="Which?")
    lines = frame(store.order_progress(), language="en", glyphs="ascii", width=50)
    header, row = lines[1], lines[2]
    assert "Skills" in header and row[header.index("Skills")] == "?"


def test_preparation_stays_when_a_short_screen_drops_other_sections(tmp_path, monkeypatch):
    from types import ModuleType

    from foragent import watch_layout

    store = StateStore(tmp_path)
    record(store, "start", title="Login")
    record(store, "ledger", add="D18 decision")
    filler = ModuleType("foragent.watch_sections.filler")
    filler.ORDER = 30
    filler.render = lambda *args, **kwargs: [[("filler", "")]] * 30
    monkeypatch.setattr(watch_layout, "_sections", lambda: [prep, filler])
    snapshot = SimpleNamespace(prep=store.order_progress(), titles={})
    config = replace(Config(), language="en", view_glyphs="ascii")
    lines = plain(watch_layout._frame(snapshot, config, None, tick=0, width=60, selected=None,
                                      now=None, height=24))
    assert lines[:3] == ["[+] Login · sorting the decisions", "D18 decision", "Please confirm these decisions."]
    # Shell: an explicit height drops nothing; the other rows stay reachable below.
    assert lines.count("filler") == 30
