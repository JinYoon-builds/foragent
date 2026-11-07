"""Acceptance: not-set-up folders name /fa as the next step (UX r1108).

In a git repository without .foragent.yml, `fa dashboard --once`,
`fa dashboard --open` and `fa brief` each print the same not-set-up message,
in the configured language, and that message names /fa as the next step.
Each command keeps its exit code (dashboard 1, brief 0).
"""

from pathlib import Path

import pytest
import yaml

from not_set_up_case import COMMANDS, HANGUL, SLASH_FA, message_lines, plain_repo, run


@pytest.mark.parametrize("language", ["en", "ko"])
@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_command_names_slash_fa_once_and_keeps_its_exit_code(tmp_path, monkeypatch, capsys, command, language):
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    root = plain_repo(tmp_path, language)
    argv, exit_code = COMMANDS[command]
    code, out, err = run(argv, root, capsys)
    lines = message_lines(out, err)
    naming = [line for line in lines if SLASH_FA.search(line)]
    assert len(naming) == 1, (command, lines)
    assert code == exit_code, (command, code, lines)


@pytest.mark.parametrize("language", ["en", "ko"])
def test_all_three_commands_print_the_same_message(tmp_path, monkeypatch, capsys, language):
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    root = plain_repo(tmp_path, language)
    printed = {command: message_lines(*run(argv, root, capsys)[1:]) for command, (argv, _) in COMMANDS.items()}
    assert printed["brief"], printed
    assert any(SLASH_FA.search(line) for line in printed["brief"]), printed
    assert printed["dashboard --once"] == printed["brief"], printed
    assert printed["dashboard --open"] == printed["brief"], printed


def test_dashboard_no_longer_says_not_a_foragent_repo(tmp_path, monkeypatch, capsys):
    root = plain_repo(tmp_path, "en")
    for argv in (["dashboard", "--once"], ["dashboard", "--open"]):
        code, out, err = run(argv, root, capsys)
        text = out + err
        assert "not a Foragent repo" not in text, text
        assert ".foragent.yml missing" not in text, text
        assert SLASH_FA.search(text), text
        assert code == 1


def test_english_message_says_not_set_up_and_where_to_type_slash_fa(tmp_path, monkeypatch, capsys):
    root = plain_repo(tmp_path, "en")
    for command, (argv, _) in COMMANDS.items():
        lines = message_lines(*run(argv, root, capsys)[1:])
        text = " ".join(lines)
        assert SLASH_FA.search(text), (command, text)
        assert "set up" in text, (command, text)
        assert "Claude Code" in text, (command, text)
        assert not HANGUL.search(text), (command, text)


def test_korean_config_gives_korean_message_whatever_lang_says(tmp_path, monkeypatch, capsys):
    """The configured language (personal config), not the shell's LANG, picks the text."""
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    root = plain_repo(tmp_path, "ko")
    for command, (argv, _) in COMMANDS.items():
        lines = message_lines(*run(argv, root, capsys)[1:])
        naming = [line for line in lines if SLASH_FA.search(line)]
        assert naming and HANGUL.search(" ".join(lines)), (command, lines)
        assert "Claude Code" in " ".join(lines), (command, lines)


def test_korean_and_english_messages_differ(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    root = plain_repo(tmp_path, "en")
    en_brief = message_lines(*run(["brief"], root, capsys)[1:])
    en_dash = message_lines(*run(["dashboard", "--once"], root, capsys)[1:])
    personal = Path.home() / ".config/foragent/config.yml"
    personal.write_text(yaml.safe_dump({"language": "ko"}))
    ko_brief = message_lines(*run(["brief"], root, capsys)[1:])
    ko_dash = message_lines(*run(["dashboard", "--once"], root, capsys)[1:])
    assert SLASH_FA.search(" ".join(en_brief)) and SLASH_FA.search(" ".join(ko_brief)), (en_brief, ko_brief)
    assert ko_brief != en_brief, (en_brief, ko_brief)
    assert en_dash == en_brief and ko_dash == ko_brief, (en_brief, en_dash, ko_brief, ko_dash)
