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


@pytest.mark.parametrize("path,function", [
    ("/customer/foragent/config.py", "load_config"),               # suffix look-alike outside the package
    (f"{PACKAGE}/../../tests/conftest.py", "git_history"),          # escapes the package after resolving
    (f"{PACKAGE}/config.py", "private_customer_function"),          # real file, function it does not define
    (f"{PACKAGE}/no_such_module.py", "run"),                        # file that does not exist
])
def test_unverified_frames_are_dropped(path, function):
    assert upstream_report.package_frames(frame(path, function)) == []


def test_verified_frames_keep_package_relative_paths():
    text = frame(f"{PACKAGE}/runner.py", "run_issue", 704) + frame(f"{PACKAGE}/config.py", "<module>", 3)
    assert upstream_report.package_frames(text) == [
        ("foragent/runner.py", 704, "run_issue"), ("foragent/config.py", 3, "<module>")]


def test_multiline_message_lines_never_become_the_error_type():
    text = ("Traceback (most recent call last):\n" + frame(f"{PACKAGE}/runner.py", "run_issue")
            + "    raise ValueError(detail)\nValueError: first line\nPRIVATE_ISSUE_TITLE: details\n")
    assert upstream_report.error_type(text) == "ValueError"


def test_free_text_type_lines_are_not_exception_types():
    text = "Traceback (most recent call last):\n" + frame("/u/app.py", "go") + "    x()\nPrivateTitle: details\n"
    assert upstream_report.error_type(text) is None
    assert upstream_report.error_type("PrivateTitle: details") is None


def test_flag_and_joined_event_agree_when_code_mentions_another_exception(tmp_path):
    from foragent.orch_events import append_event

    trace = ("Traceback (most recent call last):\n" + frame(f"{PACKAGE}/runner.py", "run_issue")
             + '    raise ValueError("KeyError: missing")\nValueError: KeyError: missing\n')
    assert upstream_report.error_type(trace) == "ValueError"
    assert upstream_report.error_type(" ".join(trace.split())) == "ValueError"
    store = StateStore(tmp_path / "state", now=lambda: datetime(2026, 10, 27, tzinfo=UTC))
    store.save_flag(1, trace)
    append_event(store, datetime(2026, 10, 27, tzinfo=UTC), 1, "error", trace)
    flag = upstream_report.propose(store, "flag:1", [])
    event = upstream_report.propose(store, "event:1", [])
    assert flag["signature"] == event["signature"]


def test_text_inside_an_unfinished_code_line_is_not_the_exception():
    text = frame("/u/app.py", "go") + "    call(PrivateTitle: 1) PrivateName: KeyError: secret"
    assert upstream_report.error_type(" ".join(text.split())) is None


def test_dotted_and_foragent_exception_types():
    assert upstream_report.error_type("subprocess.CalledProcessError: boom") == "CalledProcessError"
    assert upstream_report.error_type("foragent.config.ConfigError: bad") == "ConfigError"
    assert upstream_report.error_type("customer.PrivateError: bad") is None


@pytest.mark.parametrize("language,expected", [("en", "Error type: RuntimeError"), ("ko", "오류 종류: RuntimeError")])
def test_report_prose_follows_the_repository_language(tmp_path, language, expected):
    store = StateStore(tmp_path / "state", now=lambda: datetime(2026, 10, 27, tzinfo=UTC))
    store.save_flag(1, "Traceback (most recent call last):\n" + frame(f"{PACKAGE}/runner.py", "run_issue")
                    + "    raise RuntimeError(x)\nRuntimeError: secret\n")
    report = upstream_report.propose(store, "flag:1", ["language"], language)
    assert expected in report["body"] and report["signature"] in report["body"]
    assert "secret" not in report["title"] + report["body"]


@pytest.mark.parametrize("language,expected", [("en", "Approve sending this report"), ("ko", "보내도록 승인합니다")])
def test_report_help_follows_the_repository_language(tmp_path, monkeypatch, capsys, language, expected):
    from foragent import cli

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / ".foragent.yml").write_text(f"language: {language}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["report", "send", "--help"])
    assert exit_info.value.code == 0
    assert expected in capsys.readouterr().out
