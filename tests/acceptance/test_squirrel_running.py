"""Acceptance test for squirrel-running. Locked by Foragent; do not edit."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import re

import pytest

from test_heartbeats import heartbeats_write
from test_skills_autosync import isolated_home
from test_view_done_state import cell_count, done_snapshot


@pytest.fixture(autouse=True)
def view_offline(isolated_home, tmp_path, monkeypatch):
    """Share existing HOME isolation and forbid external I/O for the new view tests."""
    import curses
    import socket
    import subprocess
    import tempfile
    from pathlib import Path

    from foragent import executors

    def forbidden(*args, **kwargs):
        pytest.fail("lane-view acceptance must not open a terminal, network, or executor")

    monkeypatch.setenv("FORAGENT_ORCH_SPAWN", "off")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    for name in ("ORCA_TERMINAL_HANDLE", "TMUX"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(curses, "initscr", forbidden)
    monkeypatch.setattr(curses, "wrapper", forbidden)
    monkeypatch.setattr(executors.CodexExecutor, "run", forbidden)
    monkeypatch.setattr(executors.ClaudeExecutor, "run", forbidden)
    popen = subprocess.Popen

    def local_process(argv, *args, **kwargs):
        # Git fixtures and Done when's file checks are the only child processes.
        # In particular, terminal CLIs and gh cannot bypass the Python guards.
        assert isinstance(argv, (list, tuple)) and argv
        assert Path(argv[0]).name in {"git", "sh", "bash"}, argv
        return popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", local_process)


def plain_frame(value):
    """Remove only terminal transport escapes, retaining all displayed characters."""
    value = re.sub(r"\x1b\]8;[^\x1b\x07]*(?:\x1b\\|\x07)", "", value)
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", value)


def running_snapshot():
    """Extend the existing view builder with one lane for each factual state."""
    from foragent.labels import lane_display

    snapshot, config = done_snapshot(count=5)
    names = ("work", "queue", "human", "blocked", "finished")
    old_names = list(config.lanes)
    config = replace(config, repo="owner/repo", lanes={
        name: {"title": name, "emoji": "", "paths": [f"{name}/**"]} for name in names})
    rows = {}
    done = {}
    for index, (old, name) in enumerate(zip(old_names, names)):
        row = deepcopy(snapshot.status["lanes"][old])
        row.update(slug=name, lane_display=lane_display(config, name, "ko"))
        if name != "finished":
            number = 101 + index
            item = {"number": number, "title": f"Task {number}"}
            row.update(state=("working", "queued", "waiting-human", "blocked")[index],
                       issue=number, title=item["title"], last_activity_seconds=60,
                       elapsed_seconds=60, phase="implementing" if name == "work" else None,
                       queued=[item] if name == "queue" else [],
                       running=[item] if name == "work" else [])
        rows[name] = row
        done[name] = snapshot.done[old]
    status = {**snapshot.status, "lanes": rows,
              "summary": {"working": 1, "blocked": 1, "waiting_human": 1, "queued": 1},
              "active_runs": [{"number": 101, "lane": "work", "attempts": 1,
                               "last_activity_at": (snapshot.captured_at - timedelta(seconds=60)).isoformat()}]}
    return replace(snapshot, status=status, done=done, total=snapshot.completed + 4), config


def running_view_case(git_history, tmp_path, monkeypatch):
    """Use real run/log/heartbeat records when a live orchestrator is required."""
    from test_view_evidence import view_evidence_log
    from test_view_status import status_case

    case, main = status_case(git_history, tmp_path, monkeypatch, language="ko")
    case.store.update_run(1, pid=88001, phase="implementing",
                          last_activity_at=case.clock[0].isoformat())
    monkeypatch.setattr("foragent.watch.os.kill", lambda pid, signal: None)
    view_evidence_log(case, minutes=1)
    heartbeats_write(case.store, "orch", case.clock[0])
    heartbeats_write(case.store, "gate", case.clock[0])
    case.tracker.add_issue(99, "Current order")
    for number in (1, 2, 3, 4):
        case.tracker.set_parent(number, 99)
    case.tracker.add_issue(41, "Finished task", labels=["lane:docs"], state="closed")
    case.tracker.set_parent(41, 99)
    case.store.start_run(41, branch="foragent/issue-41", worktree=case.store.worktree_path(41), lane="docs")
    case.store.finish_run(41, outcome="merged")
    return case, main


@pytest.fixture(autouse=True)
def utf8_terminal(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_CTYPE", raising=False)


def display_cells(value):
    """Independent width oracle for the spec's emoji presentation of 🐿.

    The existing shared counter covers CJK, wide emoji and combining marks;
    Unicode's narrow default text presentation of 🐿 needs one extra cell here.
    """
    return sum(2 if char == "🐿" else cell_count(char) for char in value)


def issue_row(frame, number):
    """A numbered issue occurs once, on its worker/queue row, never in a second panel."""
    rows = [line for line in plain_frame(frame).splitlines()
            if re.search(rf"(?<![0-9])#{number}(?![0-9])", line)]
    assert len(rows) == 1, f"issue #{number} must occupy exactly one text row; found {len(rows)}"
    return rows[0]


def single_worker(*, phase="implementing", state="working", elapsed=720, title="Task 101"):
    """Derive isolated row facts without altering the shared running_snapshot contract."""
    snapshot, config = running_snapshot()
    row = deepcopy(snapshot.status["lanes"]["work"])
    item = {"number": 101, "title": title}
    row.update(state=state, phase=phase, title=title, elapsed_seconds=elapsed,
               running=[item] if state != "queued" else [],
               queued=[item] if state == "queued" else [],
               waiting_for=[999] if state == "blocked" else [])
    started = (snapshot.captured_at - timedelta(seconds=elapsed)).isoformat()
    runs = [] if state == "queued" else [
        {"number": 101, "lane": "work", "attempts": 1, "status": "running",
         "phase": phase, "started_at": started, "phase_started_at": started,
         "last_activity_at": snapshot.captured_at.isoformat()}]
    status = {**snapshot.status, "lanes": {"work": row}, "active_runs": runs,
              "summary": {"working": int(state == "working"), "blocked": int(state == "blocked"),
                          "waiting_human": int(state == "waiting-human"), "queued": int(state == "queued")}}
    return (replace(snapshot, status=status, done={"work": []}, completed=0, total=1,
                    worker_states={101: state}),
            replace(config, lanes={"work": config.lanes["work"]}))


def glyph_config(tmp_path, config, mode):
    """Exercise the real YAML key rather than attaching an attribute to a fake config."""
    from foragent.config import Config, load_config

    assert hasattr(Config(), "view_glyphs"), "Config must expose view_glyphs (default auto)"
    assert Config().view_glyphs == "auto", "view_glyphs must default to auto"
    (tmp_path / ".foragent.yml").write_text(f"view_glyphs: {mode}\n", encoding="utf-8")
    loaded = load_config(tmp_path)
    assert loaded.view_glyphs == mode, "the YAML glyph choice must survive config loading"
    return replace(config, view_glyphs=loaded.view_glyphs)


def test_squirrel_running_factual_states_are_single_rows_without_lane_sprites(tmp_path):
    from foragent.watch import render_frame

    snapshot, config = running_snapshot()
    frame = plain_frame(render_frame(snapshot, config, tmp_path, width=100))
    assert "▀" not in frame and "▄" not in frame, "busy views must contain text rows, no squirrel sprites"
    for name, number, glyph, phase in (("work", 101, "🐿", "코드 쓰는 중"),
                                       ("queue", 102, "⏳", "차례 기다리는 중"),
                                       ("human", 103, "✋", "기다리는 중"),
                                       ("blocked", 104, "⏳", "기다리는 중")):
        row = issue_row(frame, number)
        assert row.lstrip().startswith(glyph), f"{name} row needs state glyph {glyph}"
        assert row.index(name) < row.index(f"#{number}") < row.index(phase), "row columns must follow the decided order"
    idle = [line for line in frame.splitlines() if "finished" in line]
    assert len(idle) == 1 and idle[0].lstrip().startswith("💤"), "an idle lane needs exactly one idle row"


@pytest.mark.parametrize("state,phase,glyph,words", [
    ("working", "implementing", "🐿", "코드 쓰는 중"),
    ("working", "resolving", "🐿", "코드 쓰는 중"),
    ("working", "baseline", "🔍", "기준 재는 중"),
    ("working", "checking", "🔍", "로컬 시험 중"),
    ("working", "reviewing", "🔍", "리뷰 받는 중"),
    ("working", "ci", "🔍", "CI 시험 중"),
    ("working", "merging", "🔍", "머지 전 재확인 중"),
    ("waiting-human", "needs-human", "✋", "기다리는 중"),
    ("waiting-human", "waiting-person", "✋", "기다리는 중"),
    ("queued", None, "⏳", "차례 기다리는 중"),
    ("blocked", None, "⏳", "기다리는 중"),
    ("queued", "limit-wait", "⏳", "차례 기다리는 중"),
    ("working", "overlap-wait", "⏳", "차례 기다리는 중"),
    ("working", "test-slot-wait", "⏳", "차례 기다리는 중"),
    ("working", "baseline-wait", "⏳", "차례 기다리는 중"),
], ids=["implementing", "resolving", "baseline", "checking", "reviewing", "ci", "merging",
        "needs-human", "waiting-person", "queued", "orchestrator-blocked", "limit-wait", "overlap-wait",
        "test-slot-wait", "baseline-wait"])
def test_squirrel_running_glyph_and_phase_follow_each_workers_facts(tmp_path, state, phase, glyph, words):
    from foragent.watch import render_frame

    snapshot, config = single_worker(state=state, phase=phase)
    try:
        frame = render_frame(snapshot, config, tmp_path, width=100)
    except KeyError as error:
        raise AssertionError(f"render_frame must support the factual phase {phase or state}") from error
    row = issue_row(frame, 101)
    assert row.lstrip().startswith(glyph), f"{phase or state} must show {glyph} at the start of its row"
    assert words in row, f"{phase or state} must use the unified phase word {words}"


def test_squirrel_running_two_workers_in_one_lane_have_independent_rows(tmp_path):
    from foragent.watch import render_frame

    snapshot, config = single_worker()
    row = snapshot.status["lanes"]["work"]
    row["running"].append({"number": 105, "title": "Second worker"})
    run = {**snapshot.status["active_runs"][0], "number": 105, "phase": "reviewing"}
    snapshot.status["active_runs"].append(run)
    snapshot.status["summary"]["working"] = 2
    snapshot.total = 2
    snapshot.worker_states[105] = "working"
    frame = plain_frame(render_frame(snapshot, config, tmp_path, width=100))
    first, second = issue_row(frame, 101), issue_row(frame, 105)
    assert first != second, "two workers in one lane must occupy two different rows"
    assert first.lstrip().startswith("🐿") and "코드 쓰는 중" in first
    assert second.lstrip().startswith("🔍") and "리뷰 받는 중" in second
    assert "Task 101" in first and "Second worker" in second
    assert sum("work" in line for line in frame.splitlines()) == 2, "no extra lane road or work table"


def test_squirrel_running_columns_align_by_display_cells_with_korean_and_emoji(tmp_path):
    from foragent.watch import render_frame

    snapshot, config = single_worker(title="한글 🐿 task")
    first = snapshot.status["lanes"]["work"]
    second = deepcopy(first)
    second.update(slug="한글", lane_display="한글", issue=105, title="Go 🧩", phase="checking",
                  running=[{"number": 105, "title": "Go 🧩"}])
    snapshot.status["lanes"]["한글"] = second
    snapshot.status["active_runs"].append({**snapshot.status["active_runs"][0],
                                           "number": 105, "lane": "한글", "phase": "checking"})
    snapshot.status["summary"]["working"] = 2
    snapshot.total = 2
    config = replace(config, lanes={**config.lanes, "한글": {"title": "한글", "emoji": ""}})
    frame = render_frame(snapshot, config, tmp_path, width=100)
    a, b = issue_row(frame, 101), issue_row(frame, 105)
    assert a.lstrip().startswith("🐿") and b.lstrip().startswith("🔍"), "alignment is measured on state-led text rows"
    for left, right in (("work", "한글"), ("#101", "#105"), ("한글 🐿 task", "Go 🧩"),
                        ("코드 쓰는 중", "로컬 시험 중"), ("12분", "12분"), ("○○○○○", "🌰○○○○")):
        assert left in a and right in b, f"aligned rows must retain {left!r} and {right!r}"
        assert display_cells(a[:a.index(left)]) == display_cells(b[:b.index(right)]), f"{left}/{right} columns must align by display width"
    assert all(display_cells(line) <= 100 for line in plain_frame(frame).splitlines())


def test_squirrel_running_long_title_is_cut_with_ellipsis_before_later_columns(tmp_path):
    from foragent.watch import render_frame

    title = "긴 제목 🧩 " * 50 + "INVISIBLE_TITLE_END"
    snapshot, config = single_worker(title=title)
    frame = plain_frame(render_frame(snapshot, config, tmp_path, width=100))
    row = issue_row(frame, 101)
    assert "긴 제목" in row and "…" in row, "the title must be visibly truncated with … on its own row"
    assert row.index("#101") < row.index("긴 제목") < row.index("…") < row.index("코드 쓰는 중") < row.index("12분") < row.index("○○○○○")
    assert "INVISIBLE_TITLE_END" not in frame
    assert all(display_cells(line) <= 100 for line in frame.splitlines()), "long titles must never wrap"


@pytest.mark.parametrize("mode", ["emoji", "ascii"])
@pytest.mark.parametrize("phase,finished", [("implementing", 0), ("baseline", 0), ("checking", 1),
                                            ("reviewing", 2), ("ci", 3), ("merging", 4)])
def test_squirrel_running_acorns_show_only_finished_phases(tmp_path, phase, finished, mode):
    from foragent.watch import render_frame

    snapshot, config = single_worker(phase=phase)
    config = glyph_config(tmp_path, config, mode)
    row = issue_row(render_frame(snapshot, config, tmp_path, width=100), 101)
    done, remaining = ("🌰", "○") if mode == "emoji" else ("#", ".")
    progress = done * finished + remaining * (5 - finished)
    assert row.rstrip().endswith(progress), f"{phase} must end in five phase markers: {progress}"


@pytest.mark.parametrize("mode,glyph,progress", [("emoji", "🔍", "○○○○○"), ("ascii", "[?]", ".....")])
def test_squirrel_running_baseline_uses_checking_glyph_without_completing_local_tests(tmp_path, mode, glyph, progress):
    from foragent.watch import render_frame

    # Acceptance baseline capture can precede code. It adds no sixth acorn and
    # cannot finish the local-tests acorn before checking also finishes.
    snapshot, config = single_worker(phase="baseline")
    config = glyph_config(tmp_path, config, mode)
    row = issue_row(render_frame(snapshot, config, tmp_path, width=100), 101)
    assert row.lstrip().startswith(glyph), "baseline uses the checking glyph in both modes"
    assert "기준 재는 중" in row, "baseline capture must use its own phase word"
    markers = re.search(r"[🌰○#.]+$", row.rstrip())
    assert markers is not None and markers.group() == progress, "initial baseline leaves exactly five code/local-tests/review/CI/merge acorns unfinished"


@pytest.mark.parametrize("phase,words", [("implementing", "writing code"),
                                         ("baseline", "measuring the baseline"), ("checking", "local tests"),
                                         ("reviewing", "in review"), ("ci", "CI tests"),
                                         ("merging", "rechecking before merge"),
                                         ("overlap-wait", "waiting for a turn"),
                                         ("test-slot-wait", "waiting for a turn"),
                                         ("baseline-wait", "waiting for a turn"),
                                         ("limit-wait", "waiting for a turn")])
def test_squirrel_running_english_uses_unified_phase_words(tmp_path, phase, words):
    from foragent.watch import render_frame

    snapshot, config = single_worker(phase=phase)
    try:
        frame = render_frame(snapshot, replace(config, language="en"), tmp_path, width=100)
    except KeyError as error:
        raise AssertionError(f"render_frame must support the factual phase {phase}") from error
    row = issue_row(frame, 101)
    assert words in row and "12m" in row, f"English rows must show {words!r} and '12m'"
    assert all(word not in row for word in ("코드", "기준", "로컬", "리뷰", "시험", "재확인", "기다리는", "분"))


@pytest.mark.parametrize("language,seconds,elapsed", [("ko", 720, "12분"), ("en", 720, "12m"),
                                                      ("ko", 3540, "59분"), ("en", 3540, "59m"),
                                                      ("ko", 3600, "1시간 0분"), ("en", 3600, "1h 0m"),
                                                      ("ko", 3900, "1시간 5분"), ("en", 3900, "1h 5m")])
def test_squirrel_running_elapsed_time_uses_minutes_then_hours(tmp_path, language, seconds, elapsed):
    from foragent.watch import render_frame

    snapshot, config = single_worker(elapsed=seconds)
    row = issue_row(render_frame(snapshot, replace(config, language=language), tmp_path, width=100), 101)
    assert elapsed in row, f"{seconds}s in {language} must display {elapsed!r} on the worker row"


@pytest.mark.parametrize("mode,glyph,progress", [("ascii", "[?]", "##..."), ("emoji", "🔍", "🌰🌰○○○")])
def test_squirrel_running_explicit_glyph_config_overrides_terminal(tmp_path, monkeypatch, mode, glyph, progress):
    from foragent.watch import render_frame

    # Each explicit choice is tested in the environment that auto would choose against it.
    monkeypatch.setenv("TERM", "dumb" if mode == "emoji" else "xterm-256color")
    monkeypatch.setenv("LANG", "C" if mode == "emoji" else "en_US.UTF-8")
    snapshot, config = single_worker(phase="reviewing")
    config = glyph_config(tmp_path, config, mode)
    row = issue_row(render_frame(snapshot, config, tmp_path, width=100), 101)
    assert row.lstrip().startswith(glyph) and row.rstrip().endswith(progress), "explicit glyph mode must control state and progress"


@pytest.mark.parametrize("term,lang,lc_all,lc_ctype,ascii_mode", [
    ("dumb", "en_US.UTF-8", None, None, True),
    ("linux", "en_US.UTF-8", None, None, True),
    ("xterm-256color", "C", None, None, True),
    ("xterm-256color", "ko_KR.UTF-8", None, None, False),
    ("xterm-256color", "en_US.utf8", None, None, False),
    ("xterm-256color", "en_US.UTF-8", "C", None, True),
    ("xterm-256color", "C", "en_US.UTF-8", None, False),
    ("xterm-256color", "en_US.UTF-8", None, "C", True),
], ids=["dumb", "linux", "non-utf8", "utf8", "utf8-alias", "lc-all-C", "lc-all-utf8", "lc-ctype-C"])
def test_squirrel_running_auto_glyphs_follow_terminal_and_effective_locale(
    tmp_path, monkeypatch, term, lang, lc_all, lc_ctype, ascii_mode,
):
    from foragent.watch import render_frame

    monkeypatch.setenv("TERM", term)
    monkeypatch.setenv("LANG", lang)
    for key, value in (("LC_ALL", lc_all), ("LC_CTYPE", lc_ctype)):
        if value is not None:
            monkeypatch.setenv(key, value)
    snapshot, config = single_worker(phase="checking")
    # Leaving the key out exercises auto's default as well as its detection.
    row = issue_row(render_frame(snapshot, config, tmp_path, width=100), 101)
    glyph, progress = ("[?]", "#....") if ascii_mode else ("🔍", "🌰○○○○")
    assert row.lstrip().startswith(glyph) and row.rstrip().endswith(progress), f"auto must select {'ascii' if ascii_mode else 'emoji'} for this terminal/locale"
    explicit = glyph_config(tmp_path, config, "auto")
    assert issue_row(render_frame(snapshot, explicit, tmp_path, width=100), 101) == row, "explicit auto and the omitted default must render identically"


def test_squirrel_running_ascii_maps_all_factual_states_and_idle(tmp_path):
    from foragent.watch import render_frame

    snapshot, config = running_snapshot()
    config = glyph_config(tmp_path, config, "ascii")
    frame = plain_frame(render_frame(snapshot, config, tmp_path, width=100))
    for number, glyph in ((101, "[*]"), (102, "[.]"), (103, "[!]"), (104, "[.]")):
        assert issue_row(frame, number).lstrip().startswith(glyph), f"ASCII issue #{number} must use {glyph}"
    idle = [line for line in frame.splitlines() if "finished" in line]
    assert len(idle) == 1 and idle[0].lstrip().startswith("[z]"), "ASCII idle must use [z]"
    assert all(glyph not in frame for glyph in ("🐿", "🔍", "✋", "⏳", "💤", "🌰", "○"))


@pytest.mark.parametrize("width", [160, 100, 99, 80, 60, 59, 40, 32, 20])
def test_squirrel_running_width_rules_drop_title_then_elapsed_without_wrapping(tmp_path, width):
    from foragent.watch import render_frame

    snapshot, config = single_worker(title="TITLE_COLUMN")
    frame = plain_frame(render_frame(snapshot, config, tmp_path, width=width))
    assert all(display_cells(line) <= width for line in frame.splitlines()), f"no frame line may wrap at width {width}"
    row = issue_row(frame, 101)
    assert ("TITLE_COLUMN" in row) == (width >= 100), "the title column is shown only from 100 columns"
    assert ("12분" in row) == (width >= 60), "elapsed is shown only from 60 columns"
    assert row.lstrip().startswith("🐿"), "a narrow row still begins with its state"
    if width >= 40:
        assert "work" in row and "코드 쓰는 중" in row and "○○○○○" in row


def test_squirrel_running_twenty_workers_fit_24_rows_with_header_and_footer(tmp_path):
    from foragent.watch import render_frame

    snapshot, config = single_worker()
    template, run = snapshot.status["lanes"]["work"], snapshot.status["active_runs"][0]
    lanes, rows, runs = {}, {}, []
    for i in range(20):
        slug, number = f"lane{i:02}", 201 + i
        lanes[slug] = {"title": slug, "emoji": ""}
        row = deepcopy(template)
        row.update(slug=slug, lane_display=slug, issue=number, title=f"Task {number}",
                   running=[{"number": number, "title": f"Task {number}"}])
        rows[slug] = row
        runs.append({**run, "number": number, "lane": slug})
    snapshot.status.update(lanes=rows, active_runs=runs,
                           summary={"working": 20, "blocked": 0, "waiting_human": 0, "queued": 0})
    snapshot = replace(snapshot, done={s: [] for s in lanes}, worker_states={}, total=20)
    config = replace(config, lanes=lanes)
    # No viewport clipping: the complete frame itself must fit, with every worker visible.
    lines = plain_frame(render_frame(snapshot, config, tmp_path, width=100)).splitlines()
    assert len(lines) <= 24, f"20 lanes plus header/footer must fit 24 rows; got {len(lines)}"
    positions = [lines.index(issue_row("\n".join(lines), n)) for n in range(201, 221)]
    assert len(set(positions)) == 20 and min(positions) > 0 and max(positions) < len(lines) - 1
    assert "q" in lines[-1], "the footer must remain visible below all twenty rows"
    assert all(display_cells(line) <= 100 for line in lines)


@pytest.mark.parametrize("scene", ["first-run", "first-run-queued", "all-idle", "queued", "busy"])
def test_squirrel_running_only_one_big_squirrel_in_header_when_first_run_or_all_idle(tmp_path, scene):
    from foragent import watch

    snapshot, config = running_snapshot()
    if scene in {"first-run", "first-run-queued", "all-idle"}:
        for row in snapshot.status["lanes"].values():
            row.update(state="idle", issue=None, title=None, phase=None, queued=[], running=[])
        snapshot.status.update(active_runs=[], summary={"working": 0, "blocked": 0, "waiting_human": 0, "queued": 0})
    if scene in {"first-run", "first-run-queued"}:
        snapshot = replace(snapshot, done={s: [] for s in config.lanes}, completed=0, total=0)
        snapshot.status["history"] = []
    else:
        snapshot.status["history"] = [{"number": 41, "lane": "finished", "status": "merged"}]
    if scene in {"queued", "first-run-queued"}:
        snapshot.status["active_runs"] = []
        for row in snapshot.status["lanes"].values():
            row.update(state="idle", issue=None, title=None, phase=None, queued=[], running=[])
        snapshot.status["lanes"]["queue"].update(state="queued", issue=102, title="Task 102",
                                                  queued=[{"number": 102, "title": "Task 102"}])
        snapshot.status["summary"].update(working=0, blocked=0, waiting_human=0, queued=1)
        if scene == "first-run-queued":
            snapshot.total = 1
    frame = plain_frame(watch.render_frame(snapshot, config, tmp_path, width=100))
    art = ["".join(glyph for glyph, foreground, background in row).rstrip()
           for row in watch.half_blocks(watch.ORCHESTRATOR, watch.PALETTE)]
    visible_art = [line.strip() for line in art if line.strip()]
    if scene in {"first-run", "first-run-queued", "all-idle"}:
        painted = [line.strip() for line in frame.splitlines() if "▀" in line or "▄" in line]
        assert len(painted) == len(visible_art) and all(line.startswith(piece) for line, piece in zip(painted, visible_art)), "first-run/all-idle header must show exactly one existing orchestrator sprite"
        last_pixel = max(i for i, line in enumerate(frame.splitlines()) if "▀" in line or "▄" in line)
        first_lane = min(i for i, line in enumerate(frame.splitlines()) if any(s in line for s in config.lanes))
        assert last_pixel < first_lane, "the only sprite belongs entirely above the lane rows"
    else:
        assert "▀" not in frame and "▄" not in frame, "once runs exist, busy/queued headers must be text only"
        first_lane = min(i for i, line in enumerate(frame.splitlines()) if "work" in line or "queue" in line)
        assert first_lane == 1, "a busy or queued view has one text header before its rows"


def test_squirrel_running_header_shows_latest_say_cut_to_one_line(tmp_path):
    from foragent.watch import render_frame

    snapshot, config = single_worker()
    snapshot.events = [
        {"at": (snapshot.captured_at - timedelta(minutes=1)).isoformat(), "issue": 101,
         "kind": "retry", "summary": "OLD_RAW", "say": "OLDER_SAY"},
        {"at": snapshot.captured_at.isoformat(), "issue": 101, "kind": "retry",
         "summary": "NEW_RAW", "say": "최신 발언 " + "긴 설명 " * 60 + "\nSECOND_SAY_LINE"},
    ]
    lines = plain_frame(render_frame(snapshot, config, tmp_path, width=100)).splitlines()
    assert "최신 발언" in lines[0], "the header must show the latest orchestrator say by timestamp"
    assert "…" in lines[0] and display_cells(lines[0]) <= 100, "long header speech must be cut to one line"
    assert all(word not in "\n".join(lines) for word in ("OLDER_SAY", "OLD_RAW", "NEW_RAW", "SECOND_SAY_LINE"))
    assert not any("긴 설명" in line for line in lines[1:]), "header speech must not continue below the header"
    assert issue_row("\n".join(lines), 101) == lines[1], "the first worker follows the single header line"
