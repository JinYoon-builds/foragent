"""`fa order red` edge cases: plan paths, the fetched base, unrun tests and strict XPASS (#1725)."""

from pathlib import Path
import sys

import pytest

sys.path.append(str(Path(__file__).resolve().parent / "acceptance"))

from test_draft_red_check import DIRTY_GREETING, DRAFT, RED, check, draft_case  # noqa: E402, F401

from conftest import git_command  # noqa: E402


@pytest.mark.parametrize("name", ["../outside_test.py", "tests/../../outside_test.py", "/tmp/outside_test.py"])
def test_a_path_outside_the_copy_is_not_checked_and_writes_nothing(draft_case, capsys, name):
    root = draft_case[0]
    code, output = check(draft_case, capsys, {name: RED})
    assert code == 2, output
    assert name in output
    assert not (root.parent / "outside_test.py").exists() and not Path("/tmp/outside_test.py").exists()


def test_local_commits_the_remote_lacks_do_not_decide(draft_case, capsys):
    root = draft_case[0]
    tip = git_command(root, "rev-parse", "HEAD")
    message = "Local greeting"
    git_command(root, "fast-import", "--quiet", input=(
        f"commit refs/heads/main\ncommitter T <t@example.com> 0 +0000\ndata {len(message)}\n{message}\n"
        f"from {tip}\nM 100644 inline greeting.py\ndata {len(DIRTY_GREETING)}\n{DIRTY_GREETING}\n\ndone\n"))
    assert git_command(root, "rev-parse", "refs/heads/main") != tip
    # On the unpushed commit the draft would pass; on main as fetched it fails.
    code, output = check(draft_case, capsys, {DRAFT: RED})
    assert code == 0, output


def test_a_test_left_unrun_after_an_early_stop_is_not_red(draft_case, capsys):
    # The project command's -x and addopts are already neutralised; a test can still end the session.
    stop = "\n\ndef test_stops():\n    import pytest\n    pytest.exit('stop here')\n"
    later = "\n\ndef test_zz_later():\n    assert False\n"
    code, output = check(draft_case, capsys, {DRAFT: RED + stop + later})
    assert code == 1, output
    assert "test_stops" in output and "test_zz_later" in output


def test_a_strict_xfail_that_passes_is_not_red(draft_case, capsys):
    strict = "import pytest\n\n\n@pytest.mark.xfail(strict=True)\ndef test_strict():\n    pass\n"
    code, output = check(draft_case, capsys, {DRAFT: RED + "\n\n" + strict})
    assert code == 1, output
    assert "test_strict" in output
