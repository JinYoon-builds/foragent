"""Test preparation reads the written test from the writer's working copy, not its reply.

Offline: the writer and reviewer are the shared fake executors of ``lane_tests_case``;
only the writer's reply text is rewritten here. No real agent, signal, commit or push.
"""

from dataclasses import replace
import json

import pytest

from foragent import executors
from lane_tests_case import (CANDIDATE, CHECK, TEST_PATH, assert_no_publication, disk_plan,
                             lane_case, offline_publication, prepare)

GHOST = "tests/acceptance/test_notice_ghost.py"
HELPER = "tests/acceptance/notice_helper.py"
HELPER_TEXT = "RECEIPT = 'delivered'\n"


def reply_as(monkeypatch, files):
    """Make the writer reply with ``files`` as its acceptance_files; the tree is untouched."""
    original = executors.CodexExecutor.run

    def run(self, **kw):
        result = original(self, **kw)
        message = json.loads(result.last_message)
        if "acceptance_files" not in message:
            return result
        message["acceptance_files"] = files
        return replace(result, last_message=json.dumps(message))

    monkeypatch.setattr(executors.CodexExecutor, "run", run)


def roles(case):
    return [call["role"] for call in case.calls]


def entry(case):
    record = json.loads(next((case.store.root / "prepare").glob("*.json")).read_text())
    return next(iter(record["issues"].values()))


@pytest.mark.parametrize("reply_text", [
    CANDIDATE + "\n",
    CANDIDATE.rstrip("\n"),
    CANDIDATE + "  \n\t\n",
], ids=["extra-newline", "missing-newline", "trailing-blanks"])
def test_trailing_whitespace_difference_continues_to_review_with_the_file(
        lane_case, capsys, monkeypatch, reply_text):
    case = lane_case
    reply_as(monkeypatch, {TEST_PATH: reply_text})
    row = prepare(case, capsys)
    assert row["status"] == "reviewed", (
        f"A trailing-whitespace difference must not stop preparation: {row}")
    assert roles(case) == ["writer", "reviewer"], "Exactly one writer and one review run"
    assert case.calls[1]["candidate"] == CANDIDATE
    assert entry(case)["written"] == {TEST_PATH: CANDIDATE}, "The record keeps the file's text"
    item = disk_plan(case)["issues"][0]
    assert item["acceptance_files"] == {TEST_PATH: CANDIDATE}, (
        "The plan receives the written file's bytes, not the reply's text")
    assert item["done_when"] == [CHECK]
    assert_no_publication(case)


def test_reply_naming_files_only_continues_to_review_with_the_file(lane_case, capsys, monkeypatch):
    case = lane_case
    reply_as(monkeypatch, [TEST_PATH])
    row = prepare(case, capsys)
    assert row["status"] == "reviewed", f"A reply that names its files is enough: {row}"
    assert roles(case) == ["writer", "reviewer"]
    assert case.calls[1]["candidate"] == CANDIDATE
    assert disk_plan(case)["issues"][0]["acceptance_files"] == {TEST_PATH: CANDIDATE}


# A names-only reply is accepted (statement 2), but accepting it must not drop the
# existing stops: the named list still has to match what the writer wrote.
def assert_stopped_naming(case, row, path):
    assert row["status"] == "interrupted", f"Preparation must stop: {row}"
    assert path in (row.get("reason") or ""), f"The reason must name {path}: {row}"
    assert roles(case) == ["writer"], "No review starts after a stop, and the writer runs once"
    assert "acceptance_files" not in disk_plan(case)["issues"][0]
    assert_no_publication(case)


def test_names_only_reply_naming_a_file_that_was_not_written_stops_naming_it(
        lane_case, capsys, monkeypatch):
    case = lane_case
    reply_as(monkeypatch, [TEST_PATH, GHOST])
    row = prepare(case, capsys)
    assert_stopped_naming(case, row, GHOST)


def test_reply_naming_only_a_missing_file_stops_naming_it(lane_case, capsys, monkeypatch):
    case = lane_case
    case.writer_hook = lambda worktree: (worktree / TEST_PATH).unlink()
    reply_as(monkeypatch, [TEST_PATH])
    row = prepare(case, capsys)
    assert_stopped_naming(case, row, TEST_PATH)


def test_written_file_missing_from_a_names_only_reply_stops_naming_it(
        lane_case, capsys, monkeypatch):
    case = lane_case
    case.writer_hook = lambda worktree: (worktree / HELPER).write_text(HELPER_TEXT)
    reply_as(monkeypatch, [TEST_PATH])
    row = prepare(case, capsys)
    assert_stopped_naming(case, row, HELPER)
