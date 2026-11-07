"""Regression guard (passes on main, outside Done when): each command keeps its
exit code in a git repository without .foragent.yml, and a set-up folder is not
told to set up."""

import pytest
import yaml

from not_set_up_case import COMMANDS, SLASH_FA, message_lines, plain_repo, run


@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_exit_code_is_unchanged(tmp_path, capsys, command):
    root = plain_repo(tmp_path, "en")
    argv, exit_code = COMMANDS[command]
    code, out, err = run(argv, root, capsys)
    assert code == exit_code, (command, code, out, err)
    assert message_lines(out, err), command


def test_brief_in_a_set_up_folder_does_not_print_the_not_set_up_message(tmp_path, capsys):
    root = plain_repo(tmp_path, "en")
    (root / ".foragent.yml").write_text(yaml.safe_dump({"repo": "owner/repo", "language": "en"}))
    from foragent.cli import main

    capsys.readouterr()
    main(["brief", "--json", "--repo-root", str(root)])
    out = capsys.readouterr().out
    assert '"setup": false' not in out
    assert not SLASH_FA.search(out)
