"""`fa stop --help` tells a person how to undo the stop (UX r820).

`fa stop` is the person's emergency stop (#1492); its pair is `fa orch release`, which
`fa --help` lists only under internal commands. The stop's own output already names it;
the help a person reads before stopping must name it too.
"""

import pytest

from foragent import cli


def test_stop_help_names_how_to_resume(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["stop", "--help"])
    assert exit_info.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())  # argparse wraps lines
    assert "fa orch release" in help_text, f"fa stop --help must name the way back: {help_text}"
    assert "--why" in help_text and "--repo-root" in help_text, "Existing options stay documented"
