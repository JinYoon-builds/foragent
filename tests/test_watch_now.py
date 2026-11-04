"""r53 main-screen contract and a reproducible phone regression fixture."""

import builtins
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from foragent import cli  # noqa: F401 - initialize scheduler/status in the normal CLI order
from foragent import status, watch, watch_data, watch_now
from foragent.config import Config
from foragent.messages.squirrel import PHASE_WORDS, SHORT_WORDS, phase_word
from foragent.state import StateStore
from foragent.tracker import FakeTracker
from test_scheduler import ctx  # noqa: F401 - exercise the actual gate under its existing offline fixtures

NOW = datetime(2026, 11, 4, 2, 11, tzinfo=UTC)
REASON = "Issue text and a lane boundary need a decision."
WAIT = ("## Waiting\nReason: " + REASON + "\nNext condition: Record the decision.\n"
        "Responsible role: orchestrator\n")


def regression_fixture(root: Path):
    """Two workers, an owner handoff, three held issues and nine other lanes."""
    names = ("orch", "view", "board", "skills", "order", "docs", "adopt", "bench", "design", "main")
    config = Config(language="ko", animation=True, view_glyphs="emoji", merge_owner=True,
                    lanes={name: {"paths": [f"src/{name}/*.py"]} for name in names})
    store = StateStore(root, now=lambda: NOW)
    tracker = FakeTracker()
    for number, phase in ((1641, "checking"), (1860, "implementing")):
        tracker.add_issue(number, "진행 중인 작업", labels=["approved", "running", "lane:orch"])
        store.start_run(number, branch=f"foragent/issue-{number}", worktree="unused", lane="orch")
        store.update_run(number, phase=phase, phase_started_at=(NOW - timedelta(minutes=12)).isoformat())
    tracker.add_issue(1838, "머지 오너가 처리할 작업", labels=["approved", "lane:orch"])
    tracker.create_pr(head="foragent/issue-1838", base="main", title="PR", body="Closes #1838")
    for number in (1186, 1004, 1005):
        tracker.add_issue(number, "보류한 작업", WAIT, labels=["approved", "blocked", "lane:orch"])
    for number in (1491, 1854):
        tracker.add_issue(number, "다음 작업", labels=["approved", "ready", "lane:orch"])
    for number, lane in ((1901, "view"), (1902, "board"), (1903, "skills"), (1904, "order")):
        tracker.add_issue(number, "순서를 기다리는 작업", labels=["approved", "ready", f"lane:{lane}"])
    # The reference progress is 5 merged today + 25 still approved = 30.
    for number in range(2000, 2013):
        tracker.add_issue(number, "다음 작업", labels=["approved", "ready", "lane:skills"])
    store.refresh_local_issues(tracker.list_issues("all"), tracker.list_prs(), config.lanes, NOW)
    return config, store, tracker


def regression_snapshot(root: Path):
    config, store, tracker = regression_fixture(root)
    facts = watch_data.collect(status.collect(store, config, NOW), tracker, store, config, NOW, repo_root=root)
    facts = replace(facts, today=5, recent=2, eta=NOW + timedelta(hours=3),
                    eta_range=(NOW + timedelta(hours=3), NOW + timedelta(hours=5)),
                    park_facts={1004: {"parked": True}, 1005: {"parked": True}},
                    role_facts={"architect": {"alive": False, "last_called_at": (NOW - timedelta(minutes=37)).isoformat()},
                                "docs": {"alive": False, "last_called_at": (NOW - timedelta(minutes=56)).isoformat()}})
    payload = watch_now.project(facts, config)
    (root / watch_now.FILENAME).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return config, store, watch_now.read(store, NOW), payload


@pytest.mark.parametrize("width", [42, 100])
def test_big_squirrel_stacks_above_the_bubble_or_stays_beside_it(tmp_path, width):
    from foragent.watch_art import ORCHESTRATOR, pixel_rows
    from foragent.watch_sections.lane_bubble import SPRITE_CELLS, SPRITE_GAP

    config, _, snapshot, _ = regression_snapshot(tmp_path)
    layout = {}
    lines = watch.render_frame(snapshot, config, tmp_path, width=width, layout=layout).splitlines()
    sprite = ["".join(value for value, _ in row).rstrip() for row in pixel_rows(ORCHESTRATOR)]
    bubble_top = next(index for index, line in enumerate(lines) if "╭" in line)
    if width == 42:
        assert lines[1:1 + len(sprite)] == sprite
        assert bubble_top == 1 + len(sprite)
        assert lines[bubble_top] == "╭" + "─" * (width - 2) + "╮"
        assert lines[bubble_top + 7] == "╰" + "─" * (width - 2) + "╯"
    else:
        assert bubble_top == 1
        assert watch._cells(lines[bubble_top].split("╭")[0]) == SPRITE_CELLS + SPRITE_GAP
        assert [line[:SPRITE_CELLS].rstrip() for line in lines[bubble_top:bubble_top + len(sprite)]] == sprite
    assert (0, 1, SPRITE_CELLS, 1 + len(sprite), ("orchestrator", None)) in layout["hits"]
    assert all(watch._cells(line) <= width for line in lines)


@pytest.mark.parametrize("width,height", [(42, 30), (100, 40)])
def test_main_refresh_and_render_read_only_now(tmp_path, monkeypatch, width, height):
    config, store, _, payload = regression_snapshot(tmp_path)
    allowed = store.root / watch_now.FILENAME
    reads = []
    builtin_open, path_read, io_open = builtins.open, Path.read_text, io.open

    def check(path):
        assert Path(path) == allowed, f"Main screen tried to read {path}"
        reads.append(Path(path))

    def guarded_open(path, *args, **kwargs):
        check(path)
        return builtin_open(path, *args, **kwargs)

    def guarded_io_open(path, *args, **kwargs):
        check(path)
        return io_open(path, *args, **kwargs)

    def guarded_read(path, *args, **kwargs):
        check(path)
        return path_read(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(io, "open", guarded_io_open)
    monkeypatch.setattr(Path, "read_text", guarded_read)
    forbidden = Mock(side_effect=AssertionError("raw status/collection must not run in watch"))
    monkeypatch.setattr(watch, "collect", forbidden)
    output = []
    monkeypatch.setattr(builtins, "print", output.append)
    watch.run(forbidden, forbidden, store, config, tmp_path, once=True, now=lambda: NOW)
    snapshot = watch_now.read(store, NOW)
    state, layout = {"store": store}, {}
    frame = watch.render_frame(snapshot, config, tmp_path, width=width, height=height, state=state, layout=layout)
    # Every animation tick is pure and can scroll every lane without consulting raw records.
    count = len(reads)
    for offset in range(state["length"]):
        state["scroll"] = offset
        watch.render_frame(snapshot, config, tmp_path, tick=offset, width=width, height=height, state=state, layout=layout)
    assert len(reads) == count and set(reads) == {allowed}
    assert len(frame.splitlines()) <= height
    assert all(watch._cells(line) <= width for line in frame.splitlines())
    assert payload["summary"]["working"] == 2
    assert payload["summary"]["working_lanes"] == 1
    assert payload["summary"]["total"] == 30
    assert len(json.dumps(payload, ensure_ascii=False).encode()) < 9000
    forbidden.assert_not_called()


def test_main_contains_only_now_information_and_no_issue_explanations(tmp_path):
    config, store, snapshot, payload = regression_snapshot(tmp_path)
    state, layout = {}, {}
    frame = watch.render_frame(snapshot, config, tmp_path, width=100, state=state, layout=layout)
    for forbidden in (REASON, "기다리는 이유", "다음 조건", "담당 역할", "orchestrator", "사람이 볼 것", "요청 ",
                      "CI 결과", "진행 중인 작업", "보류한 작업"):
        assert forbidden not in frame
        assert forbidden not in json.dumps(payload, ensure_ascii=False)
    assert "17% · 이슈 30개 중 5개 완료" in frame
    assert "최근 3시간에 2개를 머지했어요" in frame
    assert all(sum(name in line for name in ("아키텍트", "문서 담당")) in {0, 2} for line in frame.splitlines())
    all_acorns = [target[1] for *_, target in layout["hits"] if target[0] == "acorn"]
    assert len(all_acorns) == len(set(all_acorns)) == 25
    assert {1186, 1004, 1005} <= {item["number"] for item in payload["lanes"]["orch"]["next"]}
    # The slot's cause is issue detail too; only its short state belongs on the main screen.
    payload["lanes"]["view"]["current"] = [{"number": 1910, "phase": "test-slot-wait", "kind": "wait"}]
    (tmp_path / "now.json").write_text(json.dumps(payload))
    frame = watch.render_frame(watch_now.read(store, NOW), config, tmp_path, width=100)
    assert "#1910 대기" in frame and "시험 자리" not in frame


def test_click_reads_reason_once_and_outside_click_restores_summary(tmp_path, monkeypatch):
    config, store, snapshot, _ = regression_snapshot(tmp_path)
    state, layout = {"store": store}, {}
    watch.render_frame(snapshot, config, tmp_path, width=100, state=state, layout=layout)
    read_issues = Mock(wraps=store.local_issues)
    monkeypatch.setattr(store, "local_issues", read_issues)
    x, y, *_, target = next(hit for hit in layout["hits"] if hit[-1] == ("acorn", 1186))
    watch.handle_mouse("left", x, y, layout, store, config, state=state)
    frame = watch.render_frame(snapshot, config, tmp_path, width=100, state=state)
    assert state["explanation_target"] == target
    assert "기다리는 이유: " + REASON in frame
    assert "다음 조건: Record the decision." in frame and "담당 역할: orchestrator" in frame
    assert frame.index(REASON) < frame.index("아키텍트")
    reads = read_issues.call_count
    assert reads > 0
    watch.render_frame(snapshot, config, tmp_path, width=42, tick=2, state=state)
    assert read_issues.call_count == reads
    watch.handle_mouse("left", -1, -1, layout, store, config, state=state)
    frame = watch.render_frame(snapshot, config, tmp_path, width=100, state=state)
    assert REASON not in frame and "이슈 2개를 작업하고 있어요" in frame


def test_owner_queue_is_a_wait_in_status_acorn_and_full_word_bubble(tmp_path):
    config, store, snapshot, payload = regression_snapshot(tmp_path)
    lane = payload["lanes"]["orch"]
    item = next(item for item in lane["current"] if item["number"] == 1838)
    assert item == {"number": 1838, "phase": "merge-owner-wait", "kind": "wait"}
    frame = watch.render_frame(snapshot, config, tmp_path, width=100)
    own_row = next(line for line in frame.splitlines() if "#1838" in line)
    assert "#1838 차례" in own_row and "질문" not in own_row
    frame = watch.render_frame(snapshot, config, tmp_path, width=100,
                               state={"store": store, "explanation_target": ("acorn", 1838)})
    assert "차례 기다리는 중" in frame and "질문" not in frame
    assert status.issue_state(["approved"], False, "pr-open", merge_owner=True) == "waiting-merge"
    assert status.issue_state(["approved"], False, "pr-open") == "waiting-human"
    assert status.issue_state(["needs-human"], False, "pr-open", merge_owner=True) == "waiting-human"


def test_clicked_phase_and_elapsed_use_the_same_current_run_record(tmp_path):
    config, store, snapshot, _ = regression_snapshot(tmp_path)
    store.update_run(1641, phase="ci", phase_started_at=(NOW + timedelta(minutes=1)).isoformat())
    frame = watch.render_frame(snapshot, config, tmp_path, width=100, now=NOW + timedelta(minutes=3),
                               state={"store": store, "explanation_target": ("acorn", 1641)})
    assert "전체 시험 중" in frame and "이 단계를 2분째 하고 있어요." in frame
    store.start_run(1838, branch="old", worktree="unused", lane="orch")
    store.update_run(1838, phase="ci", phase_started_at=(NOW - timedelta(hours=1)).isoformat())
    store.finish_run(1838, "pr-open")
    frame = watch.render_frame(snapshot, config, tmp_path, width=100,
                               state={"store": store, "explanation_target": ("acorn", 1838)})
    assert "차례 기다리는 중" in frame and "이 단계를" not in frame


def test_phase_abbreviations_are_derived_from_the_approved_full_words():
    assert {phase_word(phase, "ko") for phase in PHASE_WORDS} == {
        "코드 쓰는 중", "기준 재는 중", "목표 시험 중", "리뷰 받는 중", "전체 시험 중", "머지 전 재확인 중", "차례 기다리는 중"}
    assert {phase_word(phase, "ko", short=True) for phase in PHASE_WORDS} == {
        values["ko"] for values in SHORT_WORDS.values()}


@pytest.mark.parametrize("phase,language,full,short", [
    ("checking", "ko", "목표 시험 중", "목표"),
    ("ci", "ko", "전체 시험 중", "전체"),
    ("checking", "en", "goal tests", "goal"),
    ("ci", "en", "full tests", "full"),
])
def test_test_phase_names_reach_acorns_bubbles_rows_and_status(tmp_path, capsys, phase, language, full, short):
    from foragent.watch_sections.lanes import row_words

    config, store, _, payload = regression_snapshot(tmp_path)
    config = replace(config, language=language)
    item = next(item for item in payload["lanes"]["orch"]["current"] if item["number"] == 1641)
    item["phase"] = phase
    (tmp_path / "now.json").write_text(json.dumps(payload))
    store.update_run(1641, phase=phase)
    snapshot = watch_now.read(store, NOW, language)
    assert phase_word(phase, language) == full
    assert phase_word(phase, language, short=True) == short
    assert f"#1641 {short}" in watch.render_frame(snapshot, config, tmp_path, width=100)
    selected = watch.render_frame(snapshot, config, tmp_path, width=100,
                                  state={"store": store, "explanation_target": ("acorn", 1641)})
    assert full in selected
    assert row_words("working", phase, None, language)[1] == full
    cli._print_status(status.collect(store, config, NOW) | {"launchd": {"loaded": False}}, language)
    assert full in capsys.readouterr().out


def test_gate_publishes_one_atomic_now_file_including_early_returns(tmp_path, monkeypatch):
    config, store, _ = regression_fixture(tmp_path)
    ctx = SimpleNamespace(config=config, store=store, now=lambda: NOW, repo_root=tmp_path)
    original = watch_now.publish
    calls = []

    def counted(ctx):
        calls.append(ctx)
        original(ctx)

    monkeypatch.setattr(watch_now, "publish", counted)
    with store.tick_lock() as acquired:
        assert acquired
        with watch_now.publishing(ctx):
            pass
    assert calls == [ctx]
    assert watch_now.read(store, NOW).now_view["summary"]["working"] == 2
    assert not (tmp_path / "now.json.tmp").exists()
    prior = (tmp_path / "now.json").read_bytes()
    with pytest.raises(RuntimeError, match="tick failed"):
        with watch_now.publishing(ctx):
            raise RuntimeError("tick failed")
    assert len(calls) == 2 and (tmp_path / "now.json").read_bytes() == prior


def test_missing_or_corrupt_now_never_falls_back_to_raw_records(tmp_path, monkeypatch):
    store = StateStore(tmp_path)
    monkeypatch.setattr(store, "local_issues", Mock(side_effect=AssertionError("raw read")))
    assert watch_now.read(store, NOW).status["stale"]
    (tmp_path / "now.json").write_text("incomplete {")
    snapshot = watch_now.read(store, NOW, "ko")
    assert snapshot.status["stale"]
    assert "게이트의 현재 현황을 기다리고 있어요" in watch.render_frame(snapshot, Config(language="ko"), tmp_path)


def test_gate_is_the_single_writer_under_its_tick_lock(ctx, monkeypatch):
    from foragent import scheduler

    publish = watch_now.publish
    calls = []

    def checked(context):
        with context.store.tick_lock() as acquired:
            assert not acquired, "the publishing tick must still own the writer lock"
        calls.append(context)
        publish(context)

    monkeypatch.setattr(watch_now, "publish", checked)
    scheduler.tick(ctx)
    assert calls == [ctx] and (ctx.store.root / "now.json").exists()
    with ctx.store.tick_lock() as acquired:
        assert acquired
        scheduler.tick(ctx)
    assert calls == [ctx], "a concurrent tick must not become a second writer"
    monkeypatch.setattr(scheduler, "_github_wait", lambda *args: "waiting for GitHub")
    assert scheduler.tick(ctx).skipped == "waiting for GitHub"
    assert calls == [ctx, ctx], "early returns still publish under the gate lock"


def test_state_colours_and_words_stay_inside_acorns_without_row_reverse(tmp_path, monkeypatch):
    from test_watch_acorns import live_paint

    config, store, snapshot, payload = regression_snapshot(tmp_path)
    payload["lanes"]["view"].update(state="my-turn", current=[{"number": 1910, "phase": None, "kind": "question"}])
    (tmp_path / "now.json").write_text(json.dumps(payload))
    snapshot = watch_now.read(store, NOW)
    bodies = {}
    for tick in range(4):
        frame = watch._frame(snapshot, config, tmp_path, width=100, tick=tick, selected=None, now=NOW)
        for line in frame:
            for value, style in line:
                if value.startswith("#") and isinstance(style, tuple):
                    bodies[value] = style[2]
    assert bodies["#1641 목표"] == bodies["#1860 작성"] == 220
    assert bodies["#1838 차례"] == bodies["#1004 보류"] == 240
    assert bodies["#1910 질문"] == 203
    painted, reverse = live_paint(tmp_path, monkeypatch, snapshot, config, height=40)
    assert not reverse and any("#1838 차례" in line for line in painted.values())


def test_once_ages_the_saved_gate_heartbeat_using_the_viewer_clock(tmp_path, capsys):
    config, store, _, payload = regression_snapshot(tmp_path)
    payload["gate_age"] = 0
    (tmp_path / "now.json").write_text(json.dumps(payload))
    forbidden = Mock(side_effect=AssertionError("raw status read"))
    watch.run(forbidden, forbidden, store, config, tmp_path, once=True,
              now=lambda: NOW + timedelta(hours=1))
    assert "gate가 60분째 안 돌았어요" in capsys.readouterr().out
    forbidden.assert_not_called()


@pytest.mark.parametrize("phase,expected", [("test-slot-wait", "시험 자리 기다리는 중"),
                                             (None, "기준 재는 중")])
def test_selected_worker_reuses_the_state_specific_explanation(tmp_path, phase, expected):
    config, store, _ = regression_fixture(tmp_path)
    store.update_run(1641, phase=phase)
    watch_now.publish(SimpleNamespace(store=store, config=config, now=lambda: NOW, repo_root=tmp_path))
    snapshot = watch_now.read(store, NOW)
    frame = watch.render_frame(snapshot, config, tmp_path, width=120,
                               state={"store": store, "explanation_target": ("acorn", 1641)})
    assert expected in frame
    assert "orch 레인에서 순서를 기다려요." not in frame


def test_selected_queue_loads_open_dependency_facts(tmp_path, monkeypatch):
    config, store, _ = regression_fixture(tmp_path)
    rows = store.local_issues()
    monkeypatch.setattr(store, "local_issues", lambda: [
        dict(row, blocked_by=[{"number": 1854, "state": "open"}]) if row["number"] == 1491 else row
        for row in rows])
    watch_now.publish(SimpleNamespace(store=store, config=config, now=lambda: NOW, repo_root=tmp_path))
    snapshot = watch_now.read(store, NOW)
    frame = watch.render_frame(snapshot, config, tmp_path, width=120,
                               state={"store": store, "explanation_target": ("acorn", 1491)})
    assert "순서를 기다려요 · #1854 뒤에 해요" in frame


@pytest.mark.parametrize("target,expected", [
    (("role", "architect"), "checking lane boundaries"),
    (("role", "docs"), "checking documentation"),
    (("squirrel", "orch"), "이번 주에 1개를 마쳤어요."),
])
def test_selected_role_and_lane_load_their_explanation_records(tmp_path, monkeypatch, target, expected):
    config, store, _ = regression_fixture(tmp_path)
    for role, short in (("architect", "checking lane boundaries"), ("docs", "checking documentation")):
        (store.root / f"{role}.json").write_text(json.dumps(
            {"pid": -1, "started_at": NOW.isoformat(), "short": short}))
    store.finish_run(1860, "merged")
    memory = tmp_path / ".claude/agent-memory/fa-orch/MEMORY.md"
    memory.parent.mkdir(parents=True)
    memory.write_text("# Lessons\n- Keep lane boundaries clear\n")
    watch_now.publish(SimpleNamespace(store=store, config=config, now=lambda: NOW, repo_root=tmp_path))
    snapshot = watch_now.read(store, NOW)
    assert expected not in json.dumps(snapshot.now_view, ensure_ascii=False)
    state = {"store": store, "explanation_target": target}
    frame = watch.render_frame(snapshot, config, tmp_path, width=120, state=state)
    assert expected in frame
    if target[0] == "squirrel":
        assert "Keep lane boundaries clear" in frame and "src/orch/*.py" in frame
    with monkeypatch.context() as guards:
        guards.setattr(Path, "open", Mock(side_effect=AssertionError("detail reread")))
        watch.render_frame(snapshot, config, tmp_path, width=90, tick=2, state=state)


def test_selected_worker_keeps_the_activity_warning_and_ages_cached_facts(tmp_path, monkeypatch):
    config, store, _ = regression_fixture(tmp_path)
    silent = (NOW - timedelta(minutes=20)).isoformat()
    store.update_run(1641, last_activity_at=silent, last_progress_at=silent)
    watch_now.publish(SimpleNamespace(store=store, config=config, now=lambda: NOW, repo_root=tmp_path))
    snapshot = watch_now.read(store, NOW)
    state = {"store": store, "explanation_target": ("acorn", 1641)}
    frame = watch.render_frame(snapshot, config, tmp_path, width=120, now=NOW, state=state)
    assert "○ 멈춘 듯 20분" in frame
    with monkeypatch.context() as guards:
        guards.setattr(Path, "open", Mock(side_effect=AssertionError("detail reread")))
        frame = watch.render_frame(snapshot, config, tmp_path, width=100,
                                   now=NOW + timedelta(minutes=2), tick=2, state=state)
    assert "○ 멈춘 듯 22분" in frame


def test_snapshot_summary_uses_the_viewers_language(tmp_path):
    config, store, _ = regression_fixture(tmp_path)
    watch_now.publish(SimpleNamespace(store=store, config=config, now=lambda: NOW, repo_root=tmp_path))
    payload = json.loads((store.root / "now.json").read_text())
    assert "line" not in payload["summary"]
    english = replace(config, language="en")
    snapshot = watch_now.read(store, NOW, english.language)
    frame = watch.render_frame(snapshot, english, tmp_path, width=120)
    assert "Working on 2 issues in 1 lane." in frame
    assert "작업하고 있어요" not in frame
    assert "Common" in frame and "공통" not in frame


def test_publisher_collects_only_projection_facts_without_opening_runner_logs(tmp_path, monkeypatch):
    from foragent.watch_sections import progress

    config, store, tracker = regression_fixture(tmp_path)
    details = watch_data.collect(status.collect(store, config, NOW), tracker, store, config, NOW,
                                 repo_root=tmp_path)
    expected = watch_now.project(details, config)
    log = store.log_path(1641)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("wb") as stream:
        stream.truncate(128 * 1024 * 1024)
    original = Path.open

    def no_logs(path, *args, **kwargs):
        assert log.parent not in path.parents, f"publisher opened runner log {path}"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", no_logs)
    monkeypatch.setattr(progress, "quota", Mock(side_effect=AssertionError("discarded quota collection")))
    monkeypatch.setattr(watch_data, "meter", Mock(side_effect=AssertionError("discarded usage collection")))
    context = SimpleNamespace(store=store, config=config, now=lambda: NOW, repo_root=tmp_path)
    with store.tick_lock() as acquired:
        assert acquired
        with watch_now.publishing(context):
            pass  # An acquired tick returning early must use the same cheap publisher.
    assert json.loads((store.root / "now.json").read_text()) == expected
