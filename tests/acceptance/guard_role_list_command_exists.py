"""Regression control for N61 (passes on main; not part of Done when).

The list that `fa orch role --help` will point to is a working command.
Run explicitly: uv run pytest tests/acceptance/guard_role_list_command_exists.py -q
"""

import pytest

from foragent import cli


def test_the_named_role_list_command_exists(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["orch", "roles", "--help"])
    assert exit_info.value.code == 0
    assert "fa orch roles" in " ".join(capsys.readouterr().out.split())
