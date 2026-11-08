"""Acceptance tests for cycle-pipeline (order 2026-11-07 dashboard-click-rest).

Jin (2026-11-07) wants to see at a glance where the order cycle is stuck. Under the
progress bar, after a divider line (the same as between lanes), the dashboard shows the
eleven stages of the order cycle as chips: circled number, one space, name and count,
separated by ▶. Clicking a stage chip explains the stage in the squirrel's speech bubble.

Behaviour statements covered here:

1. Between the progress bar and the pipeline there is a divider line, and the pipeline
   shows the eleven stages in order as chips of circled number, one space, name and
   count, separated by ▶ (wrapping only at chip boundaries).
2. A stage with items, a stage with zero, the stalled stage and the merged-today stage are
   drawn in four different styles, and the stalled chip carries no cause or age text.
3. Clicking a stage chip shows in the bubble a first line with the stage, its count, its
   age when recorded and its largest reason, then its bucket counts, then the hint that
   numbers list issues.
4. The lane-waiting stage's bucket counts add up to its chip count, and its startable
   count equals the startable count elsewhere on the screen.
5. When every approved and ready issue has a hold reason this tick, the startable count is
   0 even if other queued issues have no named hold.
6. No stage is shown with an age of zero when its age is not recorded, and the pipeline is
   drawn without any GitHub call.

Everything runs through public entry points: `fa gate watch --once` (the printed
dashboard), the live `fa gate watch` on a recording curses screen, and the public
`watch.render_frame` / `watch.hit_test` / `watch.handle_mouse` click path on the snapshot
the dashboard itself drew. A chip is clicked where it is drawn; no hit-target name is
assumed. The gate's ready records are written in the format of the gate's only writer
(`scheduler_flow`: kind ``ready``, ``reason`` and ``outcome``) after the last tick: the
fixture's ticks run without the start loop, and a complete tick that never considered a
waiting issue would mark it ``released``; in a live gate the start loop notes the same
reason every tick, so the latest record per issue is what this tick decided.
"""

from __future__ import annotations

import re
import sys
from datetime import timedelta
from io import StringIO

import pytest

from helpers.cells import cells

STAGES = (("①", "기획"), ("②", "시험 준비"), ("③", "발행"), ("④", "레인 대기"), ("⑤", "구현"),
          ("⑥", "확인"), ("⑦", "리뷰"), ("⑧", "PR CI"), ("⑨", "머지 대기"), ("⑩", "묶음"),
          ("⑪", "오늘 머지"))
NUMERALS = "".join(numeral for numeral, _ in STAGES)
BUCKETS = ("바로 시작", "파일 겹침", "승인 없음", "멈춰 둠", "막힘", "후보에서 빠짐")
HINT = "숫자를 누르면 이슈 목록"
DIVIDER = "━"
WIDE = 160
ANSI = re.compile(r"\x1b\]8;[^\x1b\x07]*(?:\x1b\\|\x07)|\x1b\[[0-9;?]*[ -/]*[@-~]")


# ---------------------------------------------------------------------------------------
# Fixtures: local state the dashboard reads
# ---------------------------------------------------------------------------------------

def _ready(case, number, reason, *, minutes_ago=0):
    """One ready record exactly as the gate's tick writes it (scheduler_flow.note/hold)."""
    saved = case.clock[0]
    case.clock[0] = saved - timedelta(minutes=minutes_ago)
    try:
        case.store.record_flow("ready", issue=number, run_started_at=None, reason=reason, outcome="waiting")
    finally:
        case.clock[0] = saved


def _base(git_history, tmp_path, monkeypatch):
    """#1 is implemented in api; the web lane sits idle. Language ko (the stage names)."""
    from test_orch_review_handoff import bind_cli, make_case

    lanes = {slug: {"paths": [f"{slug}/**"], "title": slug.title(), "emoji": "🧩"} for slug in ("api", "web")}
    case = make_case(git_history, tmp_path, monkeypatch, lanes=lanes, language="ko")
    main = bind_cli(case, monkeypatch)
    case.tracker.add_labels(1, ["lane:api"])
    case.store.update_run(1, lane="api", phase="implementing")
    return case, main


EXPECTED_MIXED = {"바로 시작": 2, "파일 겹침": 2, "승인 없음": 1, "멈춰 둠": 1, "막힘": 1, "후보에서 빠짐": 1}


def _mixed(git_history, tmp_path, monkeypatch):
    """Lane-waiting work of every bucket; the oldest recorded wait is 45 minutes old.

    - #2, #3: approved and ready, no hold reason recorded: startable (바로 시작 2).
    - #4, #9: approved and ready, the gate recorded file-overlap (#4 45 minutes ago).
    - #5: ready without approval (승인 없음).
    - #6: approved and ready, parked by the orchestrator (멈춰 둠).
    - #7: approved and ready, blocked (막힘).
    - #8: approved and ready, left the start candidates without starting: the gate's
      `released` ready reason (후보에서 빠짐).
    The web lane is idle beside startable work, and #4 waits 45 minutes: ④ is stalled.
    """
    from foragent import scheduler_flow
    from foragent.scheduler import tick
    from test_orch_review_handoff import tick_context

    case, main = _base(git_history, tmp_path, monkeypatch)
    for number in (2, 3, 4, 8, 9):
        case.tracker.add_issue(number, f"Waiting {number}", labels=["approved", "ready", "lane:web"])
    case.tracker.add_issue(5, "Not approved", labels=["ready", "lane:web"])
    case.tracker.add_issue(6, "Parked", labels=["approved", "ready", "lane:web"])
    case.tracker.add_issue(7, "Blocked", labels=["approved", "ready", "blocked", "lane:web"])
    tick(tick_context(case, start_runs=False))
    assert main(["orch", "park", "6", "--why", "Wait for the design", "--repo-root", str(case.root)]) == 0
    tick(tick_context(case, start_runs=False))
    _ready(case, 4, "file-overlap", minutes_ago=45)
    _ready(case, 9, "file-overlap")
    _ready(case, 8, scheduler_flow.RELEASED)
    return case, main


EXPECTED_ALL_HELD = {"바로 시작": 0, "파일 겹침": 3, "승인 없음": 2}


def _all_held(git_history, tmp_path, monkeypatch):
    """Live check 2026-11-07 14:23: every approved and ready issue is a file-overlap hold, and
    the other queued issues (ready, not approved) have no named hold reason."""
    from foragent.scheduler import tick
    from test_orch_review_handoff import tick_context

    case, main = _base(git_history, tmp_path, monkeypatch)
    for number in (2, 3, 4):
        case.tracker.add_issue(number, f"Held {number}", labels=["approved", "ready", "lane:web"])
    for number in (5, 6):
        case.tracker.add_issue(number, f"Unapproved {number}", labels=["ready", "lane:web"])
    tick(tick_context(case, start_runs=False))
    for number in (2, 3, 4):
        _ready(case, number, "file-overlap", minutes_ago=10)
    return case, main


# ---------------------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------------------

def _plain(text):
    return ANSI.sub("", text)


class _Dashboard:
    """`fa gate watch --once` as printed, then the same dashboard driven by clicks."""

    def __init__(self, case, main, capsys, width=WIDE):
        from foragent import watch

        self.case, self.width = case, width
        seen, real = {}, watch.render_frame

        def capture(snapshot, config, root, **kwargs):
            seen.update(snapshot=snapshot, config=config, root=root)
            return real(snapshot, config, root, **kwargs)

        capsys.readouterr()
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv("COLUMNS", str(width))
            patch.setenv("LINES", "80")
            patch.setattr(watch, "render_frame", capture)
            assert main(["gate", "watch", "--once", "--repo-root", str(case.root)]) == 0
        captured = capsys.readouterr()
        assert "Traceback" not in captured.err, captured.err
        assert seen, "fa gate watch --once renders the main screen through watch.render_frame"
        self.printed = _plain(captured.out)
        self.seen, self.real = seen, real
        self.state, self.layout = {"store": case.store}, {}

    def draw(self):
        self.layout.clear()
        return _plain(self.real(self.seen["snapshot"], self.seen["config"], self.seen["root"],
                                width=self.width, state=self.state, layout=self.layout,
                                now=self.case.clock[0]))

    def click_stage(self, numeral):
        """Left-click the stage chip where the frame draws it; return the redrawn frame."""
        from foragent import watch

        frame = self.draw()
        position = _chip_position(frame, numeral)
        assert position is not None, f"the dashboard draws the stage chip {numeral}:\n{frame}"
        watch.handle_mouse("left", *position, self.layout, self.case.store, self.seen["config"],
                           state=self.state)
        return self.draw()


def _chip_position(frame, numeral):
    """(x, y) of the stage name right after the circled number, in terminal cells."""
    name = dict(STAGES)[numeral]
    for y, line in enumerate(frame.splitlines()):
        index = line.find(f"{numeral} {name}")
        if index < 0:
            index = line.find(numeral)
            if index < 0:
                continue
        target = index + len(numeral) + 1
        return cells(line[:target]) + 1, y
    return None


def _bar_index(lines):
    """The r57 progress bar row: '░… N% · 이슈 X개 중 Y개 완료'."""
    found = [index for index, line in enumerate(lines) if re.search(r"\d+% · 이슈 \d+개 중 \d+개 완료", line)]
    assert found, "the dashboard draws the progress bar:\n" + "\n".join(lines)
    return found[0]


def _is_divider(line):
    stripped = line.strip()
    return bool(stripped) and set(stripped) == {DIVIDER}


def _pipeline_rows(frame):
    """(first row index, rows) from the first circled-number row to the ⑪ row."""
    lines = frame.splitlines()
    start = next((index for index, line in enumerate(lines) if "①" in line), None)
    end = next((index for index, line in enumerate(lines) if "⑪" in line), None)
    assert start is not None and end is not None and start <= end, (
        "the dashboard draws the pipeline from ① to ⑪:\n" + frame)
    return start, lines[start:end + 1]


def _chip_pattern(numeral, name):
    return re.escape(numeral) + " " + re.escape(name) + r"[ \t]+(\d+)"


def _chips(frame):
    """{numeral: count} parsed from the pipeline, after checking order and ▶ separators."""
    _start, rows = _pipeline_rows(frame)
    flat = re.sub(r"[▐▌]", " ", "\n".join(rows))
    for numeral in NUMERALS:
        assert flat.count(numeral) == 1, f"the pipeline shows stage {numeral} exactly once: {flat!r}"
    # Chips on one row are separated by ▶; a row break between two chips may drop the arrow.
    separator = r"(?:[ \t]*▶[ \t]*(?:\n[ \t]*)?|[ \t]*\n[ \t]*(?:▶[ \t]*)?)"
    sequence = separator.join(_chip_pattern(numeral, name) for numeral, name in STAGES)
    match = re.search(sequence, flat)
    assert match, (
        "the pipeline shows the eleven stages in order as '<circled number> <name> <count>' "
        f"chips separated by ▶: {flat!r}")
    return {numeral: int(match.group(index)) for index, (numeral, _) in enumerate(STAGES, 1)}


def _bubble_rows(frame):
    """The text rows inside the speech bubble (above the progress bar), blanks dropped."""
    lines = frame.splitlines()
    rows = []
    for line in lines[:_bar_index(lines)]:
        match = re.search(r"[│┤|]\s(.*?)\s*[│|]\s*$", line)
        if match and match.group(1).strip():
            rows.append(match.group(1).strip())
    return rows


def _buckets(rows):
    """{bucket: count} from bucket rows; every item on a bucket row must be a bucket."""
    found = {}
    for row in rows:
        for item in (part.strip() for part in row.split("·")):
            match = re.fullmatch(r"(.+?)\s+(\d+)", item)
            assert match and match.group(1) in BUCKETS, (
                f"a bucket row holds only '<bucket> <count>' items, found {item!r} in {row!r}")
            assert match.group(1) not in found, f"bucket {match.group(1)} is shown once: {rows}"
            found[match.group(1)] = int(match.group(2))
    return found


def _explained(frame, numeral):
    """(first line, bucket rows) of a stage's explanation; the hint closes it."""
    rows = _bubble_rows(frame)
    name = dict(STAGES)[numeral]
    first = next((index for index, row in enumerate(rows) if row.startswith(f"{numeral} {name}")), None)
    assert first is not None, f"the bubble explains stage {numeral} on its first line, found {rows}"
    assert HINT in rows, f"the bubble ends the stage explanation with '{HINT}', found {rows}"
    hint = rows.index(HINT)
    assert first < hint, f"the stage line comes before the hint: {rows}"
    assert rows[hint + 1:] == [], f"the hint is the explanation's last line: {rows}"
    assert first == 0, f"the stage line is the bubble's first line: {rows}"
    return rows[first], rows[first + 1:hint]


class _NoGitHub:
    """Fails the test on any GitHub process; records every tracker call the view makes."""

    def __init__(self, monkeypatch, case):
        from foragent import cli, gh

        self.calls = []
        tracker = case.tracker
        calls = self.calls

        class Tracker:
            def __getattr__(self, name):
                value = getattr(tracker, name)
                if not callable(value):
                    return value

                def call(*args, **kwargs):
                    calls.append(name)
                    return value(*args, **kwargs)
                return call

        def no_github(*args, **kwargs):
            pytest.fail(f"the dashboard must not call GitHub: gh {args[:1]}")

        monkeypatch.setattr(gh, "run", no_github)
        monkeypatch.setattr(cli, "GhTracker", lambda repo: Tracker())


# ---------------------------------------------------------------------------------------
# 1. Divider line and eleven chips in order
# ---------------------------------------------------------------------------------------

def test_divider_line_between_progress_bar_and_eleven_ordered_chips(git_history, tmp_path, monkeypatch, capsys):
    case, main = _mixed(git_history, tmp_path, monkeypatch)
    dashboard = _Dashboard(case, main, capsys)
    lines = dashboard.printed.rstrip("\n").splitlines()
    bar = _bar_index(lines)
    start, _rows = _pipeline_rows(dashboard.printed)
    between = lines[bar + 1:start]
    assert between and all(_is_divider(line) for line in between), (
        "exactly divider rows lie between the progress bar and the pipeline, found "
        f"{between!r}:\n{dashboard.printed}")
    lane_dividers = [line for line in lines[start + 1:] if _is_divider(line)]
    assert lane_dividers, "lanes are separated by divider lines"
    assert between[-1].rstrip() == lane_dividers[0].rstrip(), (
        "the divider above the pipeline is the same line as between lanes")
    chips = _chips(dashboard.printed)
    assert chips["⑤"] == 1, f"⑤ 구현 counts the one issue being implemented: {chips}"
    for numeral in "⑥⑦⑧⑨":
        assert chips[numeral] == 0, f"{numeral} has nothing in it: {chips}"
    assert chips["④"] == sum(EXPECTED_MIXED.values()), f"④ 레인 대기 counts all lane-waiting work: {chips}"


def test_chips_wrap_only_at_chip_boundaries(git_history, tmp_path, monkeypatch, capsys):
    case, main = _mixed(git_history, tmp_path, monkeypatch)
    for width in (60, 80, 120):
        dashboard = _Dashboard(case, main, capsys, width=width)
        chips = _chips(dashboard.printed)
        _start, rows = _pipeline_rows(dashboard.printed)
        for numeral, name in STAGES:
            whole = f"{numeral} {name} {chips[numeral]}"
            assert any(whole in row for row in rows), (
                f"at {width} columns the chip {whole!r} stays on one row (wrap at chip boundaries): {rows}")
        wide = [row for row in rows if cells(row) > width]
        assert not wide, f"at {width} columns pipeline rows fit the screen: {wide}"
    narrow = _Dashboard(case, main, capsys, width=60)
    assert len(_pipeline_rows(narrow.printed)[1]) > 1, "at 60 columns the eleven chips wrap"


# ---------------------------------------------------------------------------------------
# 2. Four chip styles; the stalled chip carries no cause or age
# ---------------------------------------------------------------------------------------

class _TTY(StringIO):
    def isatty(self):
        return True


class _Screen:
    """A colour curses screen that records (y, x, text, attribute) of the first frame; q quits."""

    def __init__(self, height, width):
        self.size = height, width
        self.frames, self.current = [], []

    def getmaxyx(self):
        return self.size

    def addstr(self, y, x, value, attribute=0):
        self.current.append((y, x, value, attribute))

    def refresh(self):
        if self.current:
            self.frames.append(self.current)
        self.current = []

    def getch(self):
        return ord("q") if self.frames else -1

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _live_items(case, main, width=WIDE, height=80):
    """The first frame the live `fa gate watch` paints in colour, and the colour pairs."""
    import curses

    from foragent import watch

    screen, pairs = _Screen(height, width), {0: (-1, -1)}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(curses, "wrapper", lambda function, *args, **kwargs: function(screen, *args, **kwargs))
        patch.setattr(curses, "has_colors", lambda: True)
        patch.setattr(curses, "start_color", lambda: None)
        patch.setattr(curses, "use_default_colors", lambda: None)
        patch.setattr(curses, "COLORS", 256, raising=False)
        patch.setattr(curses, "COLOR_PAIRS", 256, raising=False)
        patch.setattr(curses, "init_pair", lambda index, fg, bg: pairs.__setitem__(index, (fg, bg)))
        patch.setattr(curses, "color_pair", lambda index: index << 8)
        patch.setattr(curses, "curs_set", lambda value: None)
        patch.setattr(watch, "_installed_build", lambda: "offline")
        patch.setattr(sys, "stdin", _TTY())
        patch.setattr(sys, "stdout", _TTY())
        patch.setenv("TERM", "xterm-256color")
        patch.setenv("LANG", "en_US.UTF-8")
        patch.delenv("LC_ALL", raising=False)
        patch.delenv("LC_CTYPE", raising=False)
        assert main(["gate", "watch", "--repo-root", str(case.root)]) in (0, None)
    screen.refresh()
    assert screen.frames, "the live dashboard painted a frame"
    return screen.frames[0], pairs


def _look(attribute, pairs):
    """How a span looks: its pair's colours plus every non-colour attribute bit."""
    return pairs.get((attribute >> 8) & 0xFF, ("unregistered", (attribute >> 8) & 0xFF)), attribute & ~0xFF00


def _chip_item(items, numeral):
    found = [item for item in items if numeral in item[2]]
    assert found, f"the live dashboard paints the stage chip {numeral}"
    return found[0]


def test_four_chip_styles_and_a_plain_stalled_chip(git_history, tmp_path, monkeypatch, capsys):
    case, main = _mixed(git_history, tmp_path, monkeypatch)
    chips = _chips(_Dashboard(case, main, capsys).printed)
    assert chips["⑤"] > 0 and chips["⑥"] == 0, f"fixture: ⑤ has items, ⑥ has none: {chips}"
    items, pairs = _live_items(case, main)
    looks = {name: _look(_chip_item(items, numeral)[3], pairs)
             for name, numeral in (("items", "⑤"), ("zero", "⑥"), ("stalled", "④"), ("merged", "⑪"))}
    assert len(set(looks.values())) == 4, (
        "a stage with items, a stage with zero, the stalled stage and ⑪ 오늘 머지 are drawn in four "
        f"different styles, found {looks}")
    assert _look(_chip_item(items, "⑦")[3], pairs) == looks["zero"], "every empty stage shares one style"
    # The stalled chip is the ④ chip alone: no cause, no age, no warning sign beside it.
    _start, rows = _pipeline_rows(_Dashboard(case, main, capsys).printed)
    flat = re.sub(r"[▐▌]", " ", " ".join(rows))
    chip = re.search(r"④ 레인 대기\s+\d+(.*?)▶", flat)
    assert chip, flat
    assert not chip.group(1).strip(), (
        f"the stalled chip carries no cause or age text, found {chip.group(1).strip()!r} in {flat!r}")
    for word in ("파일 겹침", "분", "이유", "⚠", "째"):
        assert word not in flat, f"no chip carries cause or age text such as {word!r}: {flat!r}"


# ---------------------------------------------------------------------------------------
# 3. Clicking a stage explains it in the bubble
# ---------------------------------------------------------------------------------------

def test_click_lane_waiting_explains_stage_age_reason_buckets_and_hint(git_history, tmp_path, monkeypatch, capsys):
    case, main = _mixed(git_history, tmp_path, monkeypatch)
    dashboard = _Dashboard(case, main, capsys)
    before = _bubble_rows(dashboard.draw())
    assert not any(row.startswith("④ 레인 대기") for row in before), "the default bubble has no stage line"
    opened = dashboard.click_stage("④")
    first, bucket_rows = _explained(opened, "④")
    total = sum(EXPECTED_MIXED.values())
    assert first == f"④ 레인 대기 {total} · 45분째 · 가장 큰 이유: 파일 겹침", (
        "the first line is the stage, its count, its recorded age and its largest reason, found "
        f"{first!r}")
    assert bucket_rows, "bucket counts follow the first line"
    assert _buckets(bucket_rows) == EXPECTED_MIXED, (
        f"the bucket counts follow the first line, found {bucket_rows}")


def test_click_each_stage_starts_with_its_line_and_ends_with_the_hint(git_history, tmp_path, monkeypatch, capsys):
    case, main = _mixed(git_history, tmp_path, monkeypatch)
    dashboard = _Dashboard(case, main, capsys)
    chips = _chips(dashboard.printed)
    for numeral, name in STAGES:
        dashboard.state = {"store": case.store}
        opened = dashboard.click_stage(numeral)
        first, _bucket_rows = _explained(opened, numeral)
        assert re.match(re.escape(f"{numeral} {name} {chips[numeral]}") + r"(?: · |$)", first), (
            f"the first line names stage {numeral}, its chip count {chips[numeral]}: {first!r}")


# ---------------------------------------------------------------------------------------
# 4. ④'s buckets add up to its chip; its startable count matches the screen
# ---------------------------------------------------------------------------------------

def _startable_numbers(frame):
    return [int(value) for value in re.findall(r"바로 시작\s+(\d+)", frame)]


def test_lane_waiting_buckets_add_up_and_startable_matches_the_screen(git_history, tmp_path, monkeypatch, capsys):
    case, main = _mixed(git_history, tmp_path, monkeypatch)
    dashboard = _Dashboard(case, main, capsys)
    chips = _chips(dashboard.printed)
    opened = dashboard.click_stage("④")
    _first, bucket_rows = _explained(opened, "④")
    buckets = _buckets(bucket_rows)
    assert set(buckets) == set(BUCKETS), f"④ shows every bucket: {buckets}"
    assert sum(buckets.values()) == chips["④"], (
        f"④'s bucket counts {buckets} add up to its chip count {chips['④']}")
    # Positive count: approved, ready, not blocked/parked/held, no hold reason recorded (#2, #3).
    assert buckets["바로 시작"] == 2, f"바로 시작 counts #2 and #3 only: {buckets}"
    for frame in (dashboard.printed, opened):
        assert set(_startable_numbers(frame)) <= {buckets["바로 시작"]}, (
            "every startable count on the screen is the same number as ④'s 바로 시작 "
            f"{buckets['바로 시작']}, found {_startable_numbers(frame)}")


# ---------------------------------------------------------------------------------------
# 5. All approved+ready issues held: startable is 0
# ---------------------------------------------------------------------------------------

def test_startable_is_zero_when_every_approved_ready_issue_is_held(git_history, tmp_path, monkeypatch, capsys):
    case, main = _all_held(git_history, tmp_path, monkeypatch)
    dashboard = _Dashboard(case, main, capsys)
    chips = _chips(dashboard.printed)
    opened = dashboard.click_stage("④")
    first, bucket_rows = _explained(opened, "④")
    buckets = _buckets(bucket_rows)
    assert buckets.get("바로 시작") == 0, (
        "with every approved and ready issue held by a recorded reason, nothing can start, even though "
        f"#5 and #6 have no named hold (never total minus holds): {buckets}")
    assert {key: value for key, value in buckets.items() if value} == {
        key: value for key, value in EXPECTED_ALL_HELD.items() if value}, buckets
    assert sum(buckets.values()) == chips["④"], (buckets, chips)
    assert first.endswith("가장 큰 이유: 파일 겹침"), first
    for frame in (dashboard.printed, opened):
        assert set(_startable_numbers(frame)) <= {0}, (
            f"every startable count on the screen is 0, found {_startable_numbers(frame)}")


# ---------------------------------------------------------------------------------------
# 6. No age 0 for unrecorded ages; no GitHub call
# ---------------------------------------------------------------------------------------

ZERO_AGE = re.compile(r"(?<!\d)0\s*(?:분|초|시간)")


def test_unrecorded_ages_never_show_as_zero(git_history, tmp_path, monkeypatch, capsys):
    """⑤ 구현 has an issue but no recorded phase time; every other stage but ④ has no age."""
    case, main = _mixed(git_history, tmp_path, monkeypatch)
    dashboard = _Dashboard(case, main, capsys)
    _start, rows = _pipeline_rows(dashboard.printed)
    assert not ZERO_AGE.search(" ".join(rows)), f"no chip shows an age of zero: {rows}"
    for numeral, _name in STAGES:
        dashboard.state = {"store": case.store}
        first, _rows = _explained(dashboard.click_stage(numeral), numeral)
        assert not ZERO_AGE.search(first), (
            f"stage {numeral} has no recorded age and is not shown with age zero: {first!r}")
        if numeral != "④":
            assert "째" not in first, f"stage {numeral} has no recorded age to show: {first!r}"


def test_pipeline_draws_and_explains_without_github(git_history, tmp_path, monkeypatch, capsys):
    case, main = _mixed(git_history, tmp_path, monkeypatch)
    guard = _NoGitHub(monkeypatch, case)
    dashboard = _Dashboard(case, main, capsys)
    _chips(dashboard.printed)
    first, _rows = _explained(dashboard.click_stage("④"), "④")
    assert first.startswith("④ 레인 대기"), first
    _chips(dashboard.draw())
    _live_items(case, main)
    assert guard.calls == [], f"redraw and click read only local state, but the tracker was called: {guard.calls}"
