"""Unit checks for upstream report extraction: only verified Foragent facts survive (#419)."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

import foragent
from foragent import upstream_report
from foragent.state import StateStore

PACKAGE = Path(foragent.__file__).resolve().parent


def frame(path, function, line=10):
    return f'  File "{path}", line {line}, in {function}\n'


HEADER = "Traceback (most recent call last):\n"


def parse(text):
    return upstream_report.parse_traceback(text)


@pytest.mark.parametrize("path,function", [
    ("/customer/foragent/config.py", "load_config"),               # suffix look-alike outside the package
    (f"{PACKAGE}/../../tests/conftest.py", "git_history"),          # escapes the package after resolving
    (f"{PACKAGE}/config.py", "private_customer_function"),          # real file, function it does not define
    (f"{PACKAGE}/no_such_module.py", "run"),                        # file that does not exist
])
def test_unverified_frames_are_dropped(path, function):
    assert parse(HEADER + frame(path, function) + "RuntimeError: x\n") == ("RuntimeError", [])


def test_verified_frames_keep_package_relative_paths():
    text = HEADER + frame(f"{PACKAGE}/runner.py", "run_issue", 300) + frame(f"{PACKAGE}/config.py", "<module>", 3)
    assert parse(text + "OSError: x\n")[1] == [
        ("foragent/runner.py", 300, "run_issue"), ("foragent/config.py", 3, "<module>")]


@pytest.mark.parametrize("function", ["PRIVATE_REPOSITORY.run_issue", "run_issue.PRIVATE", "run_issue(PRIVATE)"])
def test_frames_with_unchecked_function_text_are_dropped(tmp_path, function):
    text = HEADER + frame(f"{PACKAGE}/runner.py", function) + "RuntimeError: x\n"
    assert parse(text) == ("RuntimeError", [])
    store = store_at(tmp_path)
    store.save_flag(1, text)
    report = upstream_report.propose(store, "flag:1", [])
    assert "PRIVATE" not in report["title"] + report["body"]
    assert upstream_report._joined_traceback(" ".join(text.split()))[1] == []


def test_line_numbers_beyond_the_file_are_not_reported(tmp_path):
    text = HEADER + frame(f"{PACKAGE}/runner.py", "run_issue", 4317000) + "RuntimeError: x\n"
    assert parse(text)[1] == [("foragent/runner.py", None, "run_issue")]
    store = store_at(tmp_path)
    store.save_flag(1, text)
    report = upstream_report.propose(store, "flag:1", [])
    assert "4317" not in report["title"] + report["body"] and "foragent/runner.py in run_issue" in report["body"]


def test_keyerror_whose_source_is_an_if_header():
    text = (HEADER + frame(f"{PACKAGE}/scheduler.py", "tick") + '    if item["pid"] == runner_pid:\n'
            + "       ~~~~^^^^^^^\nKeyError: 'pid'\n")
    assert parse(text) == ("KeyError", [("foragent/scheduler.py", 10, "tick")])


def test_frame_showing_one_line_of_a_multiline_call():
    text = (HEADER + frame(f"{PACKAGE}/runner.py", "run_issue") + "    result = subprocess.run(\n"
            + "subprocess.CalledProcessError: Command '['git']' returned non-zero exit status 1.\n")
    assert parse(text)[0] == "CalledProcessError"


def test_chained_traceback_reports_the_last_exception_and_its_frames():
    text = (HEADER + frame(f"{PACKAGE}/config.py", "load_config") + "    raise\nValueError: first\n\n"
            "During handling of the above exception, another exception occurred:\n\n"
            + HEADER + frame(f"{PACKAGE}/runner.py", "run_issue") + "    raise X\nRuntimeError: second\n")
    assert parse(text) == ("RuntimeError", [("foragent/runner.py", 10, "run_issue")])


def test_traceback_without_source_lines():
    text = HEADER + frame(f"{PACKAGE}/runner.py", "run_issue") + frame(f"{PACKAGE}/config.py", "load_config") + "OSError: gone\n"
    assert parse(text) == ("OSError", [("foragent/runner.py", 10, "run_issue"), ("foragent/config.py", 10, "load_config")])


def test_message_lines_and_source_text_never_become_the_type():
    text = (HEADER + frame(f"{PACKAGE}/runner.py", "run_issue") + '    raise ValueError("KeyError: missing")\n'
            + "ValueError: first line\nPRIVATE_ISSUE_TITLE: details\n")
    assert parse(text)[0] == "ValueError"
    assert parse(HEADER + frame("/u/app.py", "go") + "    x()\nPrivateTitle: details\n")[0] is None
    assert parse("customer.PrivateError: bad")[0] is None
    assert parse("subprocess.CalledProcessError: boom")[0] == "CalledProcessError"
    assert parse("foragent.config.ConfigError: bad")[0] == "ConfigError"


def store_at(tmp_path):
    return StateStore(tmp_path / "state", now=lambda: datetime(2026, 10, 27, tzinfo=UTC))


@pytest.mark.parametrize("source", ['    if item["pid"] == runner_pid:\n', '    raise ValueError("KeyError: missing")\n', ""])
def test_flag_and_joined_event_read_the_same_structure(tmp_path, source):
    from foragent.orch_events import append_event

    trace = HEADER + frame(f"{PACKAGE}/runner.py", "run_issue") + source + "KeyError: 'pid'\n"
    store = store_at(tmp_path)
    store.save_flag(1, trace)
    append_event(store, datetime(2026, 10, 27, tzinfo=UTC), 1, "error", trace)
    flag = upstream_report.propose(store, "flag:1", [])
    event = upstream_report.propose(store, "event:1", [])
    assert flag["signature"] == event["signature"] and "KeyError" in flag["title"]


def test_joined_event_without_its_original_is_read_only_when_unambiguous(tmp_path):
    from foragent.orch_events import append_event

    store = store_at(tmp_path)
    at = datetime(2026, 10, 27, tzinfo=UTC)
    append_event(store, at, None, "error", HEADER + frame(f"{PACKAGE}/runner.py", "run_issue")
                 + '    if item["pid"] == runner_pid:\nKeyError: secret\n')
    append_event(store, at, None, "error", HEADER + frame(f"{PACKAGE}/runner.py", "run_issue")
                 + '    raise ValueError("KeyError: missing")\nValueError: KeyError: missing\n')
    assert "KeyError" in upstream_report.propose(store, "event:1", [])["title"]
    with pytest.raises(upstream_report.ReportError):
        upstream_report.propose(store, "event:2", [])


@pytest.mark.parametrize("language,expected", [("en", "Error type: RuntimeError"), ("ko", "오류 종류: RuntimeError")])
def test_report_prose_follows_the_repository_language(tmp_path, language, expected):
    store = StateStore(tmp_path / "state", now=lambda: datetime(2026, 10, 27, tzinfo=UTC))
    store.save_flag(1, "Traceback (most recent call last):\n" + frame(f"{PACKAGE}/runner.py", "run_issue")
                    + "    raise RuntimeError(x)\nRuntimeError: secret\n")
    report = upstream_report.propose(store, "flag:1", ["language"], language)
    assert expected in report["body"] and report["signature"] in report["body"]
    assert "secret" not in report["title"] + report["body"]


@pytest.mark.parametrize("language,expected", [("en", "Approve sending this report"), ("ko", "보내도록 승인해요")])
def test_report_help_follows_the_repository_language(tmp_path, monkeypatch, capsys, language, expected):
    from foragent import cli

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / ".foragent.yml").write_text(f"language: {language}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["report", "send", "--help"])
    assert exit_info.value.code == 0
    assert expected in capsys.readouterr().out
