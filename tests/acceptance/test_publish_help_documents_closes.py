"""`fa order publish --help` documents the plan field `closes` (UX r808).

#1791 added top-level `closes` to PLAN.json: close an issue resolved by a commit
({"number": N, "commit": SHA}) or folded into another ({"number": N, "into": M}).
The help is the schema a hand-written plan follows; it names every other plan field.
"""

import pytest

from foragent import cli


def test_publish_help_names_closes_and_both_forms(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["order", "publish", "--help"])
    assert exit_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "closes" in help_text, "The publish help must name the plan field 'closes'"
    line = next(part for part in help_text.split("\n\n") if "closes" in part)
    assert '"commit"' in line and '"into"' in line, f"Show both forms of a closes entry: {line}"
    assert "replaces" in help_text, "Existing fields stay documented"
