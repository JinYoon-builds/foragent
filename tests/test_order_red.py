"""`fa order red` edge cases: plan paths, the fetched base, unrun tests and strict XPASS (#1725)."""

from pathlib import Path
import sys

import pytest

from helpers.draft_red import DIRTY_GREETING, DRAFT, RED, check, draft_case  # noqa: F401

from conftest import git_command


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


def test_a_name_that_looks_like_an_option_stays_a_path(draft_case, capsys, tmp_path):
    caller = tmp_path / "caller-dir"
    caller.mkdir()
    (caller / "keep.txt").write_text("caller data")
    uses_tmp = "def test_uses_tmp(tmp_path):\n    assert False\n"
    # Whatever runs uses tmp_path, so a --basetemp option would empty the caller's folder.
    code, output = check(draft_case, capsys, {f"--basetemp={caller}": uses_tmp, DRAFT: RED + "\n\n" + uses_tmp})
    assert code != 0, output
    assert (caller / "keep.txt").read_text() == "caller data", "a plan name became a pytest option"


def test_a_name_that_selects_a_node_is_not_checked(draft_case, capsys):
    quiet = "\n\ndef test_quiet_greeting():\n    from greeting import greet\n    assert greet() == ''\n"
    # As a selector the name would run only the failing test of DRAFT and hide the passing one.
    code, output = check(draft_case, capsys, {DRAFT: RED + quiet, DRAFT + "::test_greets_the_person": RED})
    assert code == 2, output
    assert DRAFT + "::test_greets_the_person" in output


def run_plan(case, capsys, plan):
    import json

    from foragent import cli

    root, plan_path = case[0], case[2]
    plan_path.write_text(json.dumps(plan))
    capsys.readouterr()
    code = cli.main(["order", "red", str(plan_path), "--repo-root", str(root)])
    captured = capsys.readouterr()
    return code, captured.out + captured.err


@pytest.mark.parametrize("link", ["file", "folder"])
def test_a_base_link_outside_the_copy_is_not_run(draft_case, capsys, tmp_path, link):
    from helpers.draft_red import draft

    root, history = draft_case[0], draft_case[1]
    caller = tmp_path / "caller-only"
    (caller / "deep").mkdir(parents=True)
    (caller / "test_caller.py").write_text("def test_caller():\n    assert False\n")
    (caller / "deep" / "test_caller.py").write_text("def test_caller():\n    assert False\n")
    path, target, named = (("tests/acceptance/test_linked.py", caller / "test_caller.py", "tests/acceptance/test_linked.py")
                           if link == "file" else
                           ("tests/acceptance/linked", caller / "deep", "tests/acceptance/linked/test_caller.py"))
    message, target = "Link", str(target)
    git_command(history.origin, "fast-import", "--quiet", input=(
        f"commit refs/heads/main\ncommitter T <t@example.com> 0 +0000\ndata {len(message)}\n{message}\n"
        f"from {history.tip}\nM 120000 inline {path}\ndata {len(target)}\n{target}\n\ndone\n"))
    plan = draft({DRAFT: RED})
    plan["edits"][0]["done_when"].append(f"uv run pytest -q {named}")
    code, output = run_plan(draft_case, capsys, plan)
    # The caller-only test fails, so running it would wrongly report red.
    assert code == 2, output
    assert path in output


UNITTEST = "import unittest\n\nfrom greeting import greet\n\n\nclass Greeting(unittest.TestCase):\n"
NOT_IN_BODY = {
    "setup_raises": UNITTEST + "    def setUp(self):\n        raise RuntimeError('setup broke')\n\n"
                               "    def test_greets(self):\n        self.assertEqual(greet(), 'hello')\n",
    "teardown_raises_after_a_pass": UNITTEST + "    def tearDown(self):\n        raise RuntimeError('teardown broke')\n\n"
                                               "    def test_greets(self):\n        self.assertEqual(greet(), '')\n",
    "cleanup_raises_after_a_pass": UNITTEST + "    def test_greets(self):\n"
                                              "        self.addCleanup(lambda: 1 / 0)\n        self.assertEqual(greet(), '')\n",
    "expected_failure_passes": UNITTEST + "    @unittest.expectedFailure\n"
                                          "    def test_greets(self):\n        self.assertEqual(greet(), '')\n",
}


@pytest.mark.parametrize("case", sorted(NOT_IN_BODY))
def test_a_unittest_failure_outside_the_test_body_is_not_red(draft_case, capsys, case):
    code, output = check(draft_case, capsys, {DRAFT: NOT_IN_BODY[case]})
    assert code == 1, f"{case}:\n{output}"
    assert "test_greets" in output


@pytest.mark.parametrize("body", [
    UNITTEST + "    def test_greets(self):\n        self.assertEqual(greet(), 'hello')\n",
    "from greeting import greet\n\n\ndef expect(value):\n    assert value == 'hello'\n\n\n"
    "def test_greets():\n    expect(greet())\n",
], ids=["unittest_body", "helper_called_by_the_body"])
def test_a_failure_raised_through_the_test_body_is_red(draft_case, capsys, body):
    code, output = check(draft_case, capsys, {DRAFT: body})
    assert code == 0, output
