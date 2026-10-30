"""Independent review, real red evidence, and bounded holds before publication."""

import pytest

from lane_tests_case import (CANDIDATE, CHECK, STATEMENT, TEST_PATH, assert_no_publication,
                             disk_plan, lane_case, offline_publication, prepare)


def test_reviewed_tests_and_commands_become_the_plan_acceptance_files(lane_case, capsys):
    case = lane_case
    row = prepare(case, capsys)
    assert row["status"] == "reviewed" and row["questions"] == []
    assert [call["role"] for call in case.calls] == ["writer", "reviewer"]
    writer, reviewer = case.calls
    assert reviewer["executor"] == "claude" and reviewer["model"] == "reviewer-model"
    assert reviewer.get("read_only") is True
    assert reviewer["candidate"] == CANDIDATE
    for fact in (STATEMENT, "wrong value", "skip", "twice", "fake process"):
        assert fact in reviewer["prompt"], fact
    item = disk_plan(case)["issues"][0]
    assert item["acceptance_files"] == {TEST_PATH: CANDIDATE}
    assert item["done_when"] == [CHECK] and item["statements"] == [STATEMENT]
    assert row["base_revision"] == case.git.head_sha()
    assert row["coverage"] and row["red"]
    assert reviewer["check_result"].returncode == 1
    assert "AssertionError: 0 != 1" in reviewer["check_result"].stderr
    assert row["red"][0]["exit_code"] == reviewer["check_result"].returncode
    assert_no_publication(case)


@pytest.mark.parametrize("kind,candidate", [
    ("syntax", "def invalid(:\n"),
    ("import", "import missing_foragent_notice_dependency\n"),
    ("zero_tests", "import unittest\nunittest.main()\n"),
    ("skip", "import unittest\n@unittest.skip('later')\nclass Delivery(unittest.TestCase):\n def test_it(self): assert False\nunittest.main()\n"),
    ("unconditional", "assert False, 'not a behaviour check'\n"),
])
def test_red_for_the_wrong_reason_never_gets_promoted(lane_case, capsys, kind, candidate):
    case = lane_case
    case.candidate, case.review_kind = candidate, kind
    case.verdicts = ["hold", "hold"]
    prepare(case, capsys)
    row = prepare(case, capsys)
    assert row["status"] == "question" and row["questions"]
    assert "acceptance_files" not in disk_plan(case)["issues"][0]
    assert_no_publication(case)


def test_second_hold_for_same_issue_becomes_a_question_and_stops(lane_case, capsys):
    case = lane_case
    case.verdicts = ["hold", "hold"]
    prepare(case, capsys)
    row = prepare(case, capsys)
    assert row["status"] == "question" and row["questions"]
    assert row["holds"] == 2
    before = len(case.calls)
    assert prepare(case, capsys)["status"] == "question"
    assert len(case.calls) == before, "A third automatic rewrite needs a planning answer"
    assert "acceptance_files" not in disk_plan(case)["issues"][0]
    assert_no_publication(case)


def test_reviewer_question_is_sent_to_planning_without_changing_statements(lane_case, capsys):
    case = lane_case
    case.verdicts = ["question"]
    row = prepare(case, capsys)
    assert row["status"] == "question" and row["questions"] == ["Which delivery is meant?"]
    assert disk_plan(case)["issues"][0]["statements"] == [STATEMENT]
    assert_no_publication(case)
