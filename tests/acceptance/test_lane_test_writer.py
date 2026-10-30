"""The lane authors tests in preparation, with production left read-only."""

from pathlib import Path

import pytest

from lane_tests_case import (NOTICE, STATEMENT, TEST_PATH, assert_no_publication,
                             disk_plan, lane_case, offline_publication, prepare)


def test_writer_uses_lane_setup_and_scratch_without_implementing(lane_case, capsys):
    case = lane_case
    prepare(case, capsys)
    writers = [call for call in case.calls if call["role"] == "writer"]
    assert len(writers) == 1
    call = writers[0]
    assert call["executor"] == "codex" and call["model"] == "lane-writer-model"
    assert Path(call["worktree"]).resolve() != case.root.resolve()
    for fact in (STATEMENT, "receipt delivery", "fake process", "never send real signals"):
        assert fact in call["prompt"], fact
    assert (case.root / "notice.py").read_text() == NOTICE
    assert not (case.root / TEST_PATH).exists(), "Tests reach main only at publication"
    assert_no_publication(case)


def test_untestable_statement_returns_question_without_rewriting_the_goal(lane_case, capsys):
    case = lane_case
    question = "Which observable result should replace 'feels friendly'?"
    case.writer_questions = [question]
    before = case.path.read_bytes()
    row = prepare(case, capsys)
    assert row["status"] == "question" and row["questions"] == [question]
    assert len(case.calls) == 1 and case.calls[0]["role"] == "writer"
    assert case.path.read_bytes() == before
    assert_no_publication(case)


@pytest.mark.parametrize("path", ["notice.py", "pyproject.toml", "tests/conftest.py"])
def test_non_test_changes_are_rejected_without_reaching_main(lane_case, capsys, path):
    case = lane_case
    before = (case.root / path).read_bytes() if (case.root / path).exists() else None

    def edit(worktree):
        target = worktree / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# writer attempted an implementation/configuration edit\n")

    case.writer_hook = edit
    row = prepare(case, capsys)
    assert row["status"] == "question" and row["questions"]
    assert path in " ".join(row["questions"])
    assert "acceptance_files" not in disk_plan(case)["issues"][0]
    assert ((case.root / path).read_bytes() if (case.root / path).exists() else None) == before
    assert_no_publication(case)


def test_test_symlink_cannot_replace_a_source_file(lane_case, capsys):
    case = lane_case

    def link(worktree):
        target = worktree / TEST_PATH
        target.unlink()
        target.symlink_to(case.root / "notice.py")

    case.writer_hook = link
    row = prepare(case, capsys)
    assert row["status"] == "question" and row["questions"]
    assert (case.root / "notice.py").read_text() == NOTICE
    assert "acceptance_files" not in disk_plan(case)["issues"][0]
    assert_no_publication(case)
