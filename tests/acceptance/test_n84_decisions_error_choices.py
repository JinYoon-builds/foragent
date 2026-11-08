"""n84: `fa decisions` invalid-choice errors list only the public actions.

The internal `_sweep` hook action stays callable but never appears in usage or error output.
Regression controls that already pass on main live in test_n84_decisions_error_choices_guard.py.
"""

import io
import re

import pytest

from foragent import cli

PUBLIC = ("add", "stop-check", "drop")


def _run(argv, capsys, monkeypatch, stdin=""):
    monkeypatch.delenv("FORAGENT_ROLE", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    try:
        code = cli.main(argv)
    except SystemExit as stop:
        code = stop.code
    out, err = capsys.readouterr()
    return code, out, err


def _choice_tokens(text):
    """Words offered as choices: brace groups in usage and the 'choose from' list."""
    groups = re.findall(r"\{([^{}]*)\}", text)
    groups += re.findall(r"choose from ([^)\n]*)", text)
    tokens = []
    for group in groups:
        tokens += [part.strip().strip("'\"") for part in re.split(r"[,\s]+", group) if part.strip()]
    return tokens


@pytest.mark.parametrize("bad", ["bogus", "sweep"])
def test_invalid_action_error_lists_only_public_actions(bad, capsys, monkeypatch, tmp_path):
    code, out, err = _run(["decisions", "--repo-root", str(tmp_path), bad], capsys, monkeypatch)
    output = out + err
    assert code == 2, output
    assert "_sweep" not in output, output
    assert bad in err, err
    # The invalid-choice message itself (not only usage) names each public action once per list.
    message = [line for line in err.splitlines() if "invalid choice" in line or bad in line]
    assert message, err
    tokens = _choice_tokens(err)
    for action in PUBLIC:
        assert action in tokens, (action, err)
    assert set(tokens) <= set(PUBLIC), err
    choose = re.search(r"choose from ([^)\n]*)", err)
    if choose:
        listed = [part.strip().strip("'\"") for part in choose.group(1).split(",") if part.strip()]
        assert sorted(listed) == sorted(PUBLIC), err

