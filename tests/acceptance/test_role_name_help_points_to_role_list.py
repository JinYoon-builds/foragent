"""`fa orch role --help` points the name argument to a real list of role names (N61).

`fa role` only manages roles; the configured names are listed by `fa orch roles` and
live in the `roles` list of `.foragent.yml`. The name help must point to one of those.
"""

import re

import pytest

from foragent import cli, i18n

# A reference to the role list: `fa orch roles`, or the `roles` list in `.foragent.yml`.
ROLE_LIST = re.compile(r"\bfa orch roles(?![A-Za-z])|\.foragent\.yml(?![A-Za-z]).*\broles(?![A-Za-z])"
                       r"|\broles(?![A-Za-z]).*\.foragent\.yml(?![A-Za-z])")
# The management command named on its own as the place to list names (not `fa orch roles`).
BARE_FA_ROLE = re.compile(r"\bfa role(?![A-Za-z])")


def _help(capsys, argv: list[str]) -> str:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(argv)
    assert exit_info.value.code == 0, f"{' '.join(argv)} must exit 0"
    return capsys.readouterr().out


def _name_help(help_text: str) -> str:
    lines = help_text.splitlines()
    section = lines.index("positional arguments:")  # skip the wrapped usage line naming `name`
    start = next(i for i, line in enumerate(lines)
                 if i > section and re.match(r"^\s{2}name(\s|$)", line))
    block = [lines[start]]
    for line in lines[start + 1:]:
        # argparse continues a wrapped description on deeper-indented lines.
        if not line.strip() or re.match(r"^\s{0,4}\S", line):
            break
        block.append(line)
    return " ".join(" ".join(block).split()[1:])


def test_name_help_points_to_an_existing_role_list(capsys):
    name_help = _name_help(_help(capsys, ["orch", "role", "--help"]))
    assert name_help, "fa orch role --help must describe the name argument"
    assert ROLE_LIST.search(name_help), (
        f"name help must point to fa orch roles or .foragent.yml roles: {name_help!r}")
    assert not BARE_FA_ROLE.search(name_help), (
        f"name help must not send readers to fa role, which only manages roles: {name_help!r}")


@pytest.mark.parametrize("language", ["en", "ko"])
def test_role_name_help_catalogue_points_to_role_list(language):
    message = " ".join(i18n.text("role_name_help", language).split())
    assert ROLE_LIST.search(message), (
        f"{language} role-name help must point to fa orch roles or .foragent.yml roles: {message!r}")
    assert not BARE_FA_ROLE.search(message), (
        f"{language} role-name help must not point to fa role for names: {message!r}")

