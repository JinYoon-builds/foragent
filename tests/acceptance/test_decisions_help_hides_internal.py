"""`fa decisions --help` shows only the commands a person uses (UX r856).

The internal `_sweep` worker action is hidden from the action list, but argparse still
prints it in the usage line and the positional choices ('{_sweep,add,stop-check,drop}').
"""

import pytest

from foragent import cli


def test_decisions_help_lists_only_person_actions(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["decisions", "--help"])
    assert exit_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "_sweep" not in help_text, f"The internal worker action must not appear: {help_text}"
    for action in ("add", "stop-check", "drop"):
        assert action in help_text, f"Person action {action} stays listed"
