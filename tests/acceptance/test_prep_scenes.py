"""Acceptance test for prep-scenes. Locked by Foragent; do not edit."""

import json

from test_local_truth import record_gh
from test_orch_review_handoff import bind_cli, make_case, tick_context
from test_skills_autosync import cli_exit, isolated_home
from test_squirrel_running import issue_row, plain_frame, running_snapshot, view_offline


def preparation_case(git_history, tmp_path, monkeypatch):
    from foragent.scheduler import tick

    case = make_case(git_history, tmp_path, monkeypatch, language="ko", skill_libraries=[],
                     test_command="true")
    case.tracker.add_issue(151, "Published login", labels=["approved", "ready"])
    case.tracker.add_issue(150, "Login order")
    case.tracker.set_parent(151, 150)
    tick(tick_context(case, start_runs=False))
    return case, bind_cli(case, monkeypatch)


def progress_command(case, main, *args):
    argv = ["order", "progress", *args, "--repo-root", str(case.root)]
    assert cli_exit(main, argv) == 0, f"fa order progress must record local preparation: {args}"


def preparation_snapshot(case, main, capsys):
    from foragent.watch import collect

    capsys.readouterr()
    assert main(["gate", "status", "--json", "--repo-root", str(case.root)]) == 0
    status = json.loads(capsys.readouterr().out)
    return collect(status, case.tracker, case.store, case.config, case.clock[0])


def preparation_frame(case, main, capsys):
    from foragent.watch import render_frame

    return plain_frame(render_frame(preparation_snapshot(case, main, capsys), case.config,
                                    case.root, width=100, now=case.clock[0]))


def preparation_row(frame, title="Login 준비"):
    lines = frame.splitlines()
    assert any(title in line for line in lines), "the CLI preparation title must appear in a text row"
    assert "▀" not in frame and "▄" not in frame, "preparation and lanes must use text rows without sprites"
    worker = issue_row(frame, 1)
    rows = [line for line in lines[:lines.index(worker)] if title in line]
    assert len(rows) == 1, "the order title must appear in exactly one preparation row above the lanes"
    return rows[0]


def test_prep_scenes_ledger_uses_cli_state_and_asks_for_confirmation_on_the_left(
    git_history, tmp_path, monkeypatch, capsys,
):
    # Keep this test identifier so the importer baseline remains directly comparable.
    case, main = preparation_case(git_history, tmp_path, monkeypatch)
    calls = record_gh(monkeypatch)
    progress_command(case, main, "start", "--title", "Login 준비")
    progress_command(case, main, "ledger", "--add", "D18 작업 안의 결정은 문서에서 읽어요")
    progress_command(case, main, "ledger", "--add", "D64 진행을 실제 상태로 보여 줘요")
    frame = preparation_frame(case, main, capsys)
    preparation_row(frame)
    for value in ("D18 작업 안의 결정은 문서에서 읽어요", "D64 진행을 실제 상태로 보여 줘요", "확인해 주세요"):
        assert any(value in line for line in frame.splitlines()), f"CLI ledger/confirmation must be visible as text: {value}"
    assert calls == []


def test_prep_scenes_chunk_table_counts_actual_empty_slots_and_marks_the_asked_cell(
    git_history, tmp_path, monkeypatch, capsys,
):
    from test_view_done_state import cell_count

    case, main = preparation_case(git_history, tmp_path, monkeypatch)
    calls = record_gh(monkeypatch)
    progress_command(case, main, "start", "--title", "Login 준비")
    progress_command(case, main, "chunk", "--add", "Login")
    chunks = preparation_frame(case, main, capsys)
    assert "남은 빈칸 5" in chunks, "a new chunk has five actual empty slots"
    progress_command(case, main, "cell", "--chunk", "Login", "--slot", "goal", "--filled")
    progress_command(case, main, "cell", "--chunk", "Login", "--slot", "lane", "--ask", "어느 레인에 둘까요?")
    frame = preparation_frame(case, main, capsys)
    preparation_row(frame)
    assert "남은 빈칸 4" in frame
    labels = ("목표", "안 할 것", "Done when", "레인", "스킬")
    header = next((line for line in frame.splitlines() if all(label in line for label in labels)), "")
    assert header, "the chunk table must retain all five slot columns"
    asked = next((line for line in frame.splitlines() if "Login" in line and "?" in line), "")
    assert asked, "the asked chunk cell must contain ?"
    asked_column = cell_count(asked[:asked.index("?")])
    assert cell_count(header[:header.index("레인")]) <= asked_column < cell_count(header[:header.index("스킬")]), "? must mark the asked lane cell, not another empty slot"
    assert "어느 레인에 둘까요?" in frame
    for slot in ("out_of_scope", "done_when", "lane", "skills"):
        progress_command(case, main, "cell", "--chunk", "Login", "--slot", slot, "--filled")
    filled = preparation_frame(case, main, capsys)
    preparation_row(filled)
    assert "남은 빈칸 0" in filled
    assert not any("Login" in line and "?" in line for line in filled.splitlines())
    assert "어느 레인에 둘까요?" not in filled
    assert calls == []


def test_prep_scenes_tests_check_approval_and_publish_keep_the_same_view_and_number_only_on_publish(
    git_history, tmp_path, monkeypatch, capsys,
):
    from foragent import watch_open

    case, main = preparation_case(git_history, tmp_path, monkeypatch)
    opened = []
    monkeypatch.setattr(watch_open, "open_view", lambda *args, **kwargs: opened.append(args))
    calls = record_gh(monkeypatch)
    progress_command(case, main, "start", "--title", "Login 준비")
    opening_count = len(opened)
    progress_command(case, main, "tests", "--done", "3", "--total", "20")
    frame = preparation_frame(case, main, capsys)
    row = preparation_row(frame)
    assert "3/20" in row and "#151" not in row, "unpublished preparation shows a test count without an issue number"
    progress_command(case, main, "check")
    checking = preparation_frame(case, main, capsys)
    preparation_row(checking)
    assert "실패하는지 확인" in checking
    progress_command(case, main, "approval")
    approval = preparation_frame(case, main, capsys)
    preparation_row(approval)
    assert "승인" in approval
    progress_command(case, main, "publish", "--issue", "151")
    published = preparation_frame(case, main, capsys)
    row = preparation_row(published)
    assert "#151" in row, "only published preparation gets its issue number on the preparation row"
    assert "Published login" in published, "published details must include the local issue title"
    assert len(opened) == opening_count, "publishing continues in the preparation screen"
    assert calls == []


def test_prep_scenes_absent_preparation_state_keeps_the_normal_running_scene(tmp_path):
    from foragent.watch import render_frame

    snapshot, config = running_snapshot()
    frame = plain_frame(render_frame(snapshot, config, tmp_path, width=100))
    assert "▀" not in frame and "▄" not in frame, "the normal busy scene must use text rows without sprites"
    for number in (101, 102, 103, 104):
        row = issue_row(frame, number)
        assert row.lstrip().startswith(("🐿", "⏳", "✋", "[*]", "[.]", "[!]")), "normal lane rows keep their state glyphs"
    assert "남은 빈칸" not in frame and "확인해 주세요" not in frame
