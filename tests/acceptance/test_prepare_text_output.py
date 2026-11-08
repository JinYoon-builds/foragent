"""Plain-text `fa order prepare` prints each issue row without a formatter collision.

Offline: the writer and reviewer are the shared fake executors of ``lane_tests_case``;
the writer asks one question so the row carries a key, status, reason and question.
No real agent, GitHub call, signal, commit or push.
"""

from dataclasses import replace
import json

import pytest

from foragent import cli as cli_module
from lane_tests_case import (assert_no_publication, invoke, lane_case,  # noqa: F401
                             offline_publication)

QUESTION = "Which receipt format is meant?"


def _run(case, capsys, *args):
    """Run the CLI; an escaping exception is reported as data so the test fails by assertion."""
    try:
        code, out, err = invoke(case, capsys, *args)
    except Exception as exc:  # noqa: BLE001 - the defect is an escaping TypeError
        captured = capsys.readouterr()
        return None, captured.out, captured.err + f"{type(exc).__name__}: {exc}"
    return code, out, err


def _setup(case, monkeypatch, language):
    case.writer_questions = [QUESTION]
    config = replace(case.config, language=language)
    monkeypatch.setattr(cli_module, "load_config", lambda root: config)
    return config


def _row_lines(out, key):
    return [line for line in out.splitlines() if line.startswith(f"{key}:")]


def _question_lines(out, question):
    return [line for line in out.splitlines() if question in line]


def _assert_plain_row(out, err, code, row):
    assert code == 0, f"Plain prepare must exit 0: code={code}\n{out}{err}"
    assert "Traceback" not in out + err and "TypeError" not in out + err, out + err
    lines = _row_lines(out, row["key"])
    assert len(lines) == 1, f"Exactly one text row for {row['key']}: {out!r}"
    line = lines[0]
    assert row["status"] in line, f"The row names its status {row['status']}: {line!r}"
    assert row["reason"] and row["reason"] in line, f"The row names its reason: {line!r}"
    assert row["questions"], f"The fixture's preparation must carry a question: {row}"
    for question in row["questions"]:
        assert len(_question_lines(out, question)) == 1, (
            f"Each question is printed exactly once: {question!r} in {out!r}")


@pytest.mark.parametrize("language", ["en", "ko"])
def test_plain_prepare_prints_row_and_question_without_traceback(
        lane_case, capsys, monkeypatch, language):
    case = lane_case
    _setup(case, monkeypatch, language)
    code, out, err = _run(case, capsys, "prepare", str(case.path))
    assert code == 0, f"Plain prepare must exit 0 without a TypeError: code={code}\n{out}{err}"
    # The same saved record, read back as JSON, is the oracle for the printed values.
    json_code, json_out, json_err = _run(case, capsys, "prepare", str(case.path), "--status", "--json")
    assert json_code == 0, json_out + json_err
    row = json.loads(json_out)["issues"][0]
    assert row["key"] == "notice" and row["status"] == "question", row
    assert row["questions"] == [QUESTION], row
    _assert_plain_row(out, err, code, row)
    writers = [call for call in case.calls if call["role"] == "writer"]
    assert len(writers) == 1, "Plain prepare runs the writer once"
    assert_no_publication(case)


@pytest.mark.parametrize("language", ["en", "ko"])
def test_status_text_and_status_json_report_the_same_row(lane_case, capsys, monkeypatch, language):
    case = lane_case
    _setup(case, monkeypatch, language)
    first_code, first_out, first_err = _run(case, capsys, "prepare", str(case.path), "--json")
    assert first_code == 0, first_out + first_err
    calls = len(case.calls)

    json_code, json_out, json_err = _run(case, capsys, "prepare", str(case.path), "--status", "--json")
    assert json_code == 0, json_out + json_err
    rows = json.loads(json_out)["issues"]
    assert len(rows) == 1 and rows[0]["key"] == "notice", rows
    assert rows[0]["questions"] == [QUESTION], rows[0]

    code, out, err = _run(case, capsys, "prepare", str(case.path), "--status")
    _assert_plain_row(out, err, code, rows[0])
    assert len(case.calls) == calls, "--status starts no agent"

    again_code, again_out, _ = _run(case, capsys, "prepare", str(case.path), "--status", "--json")
    assert again_code == 0 and json.loads(again_out)["issues"] == rows, (
        "The text status query leaves the saved preparation state unchanged")
    assert_no_publication(case)
