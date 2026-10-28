"""Launch A6: current-user defaults and a safe-to-copy documented example."""

from pathlib import Path
import re

import yaml

from test_init import setup
from test_init_role_agents import init_project
from test_skills_autosync import cli_exit, isolated_home


ROOT = Path(__file__).resolve().parents[2]


def test_init_uses_authenticated_user_and_docs_do_not_copy_maintainer(
    setup, init_project, monkeypatch,
):
    from foragent import cli
    from foragent.config import load_config

    original = setup.run_gh
    calls = []

    def gh_run(args, input=None):
        calls.append(list(args))
        if args == ["api", "user", "--jq", ".login"]:
            return "new-project-owner\n"
        return original(args, input=input)

    monkeypatch.setattr(setup, "run_gh", gh_run)
    assert cli_exit(cli.main, ["init", "--repo-root", str(setup.root), "--no-push",
                               "--no-launchd", "--no-board", "--statusline", "no"]) == 0
    assert ["api", "user", "--jq", ".login"] in calls
    assert load_config(setup.root).approvers == ["new-project-owner"]

    # Check YAML examples, not historical prose, links or attribution.
    doc = (ROOT / "docs/gate.md").read_text(encoding="utf-8")
    examples = [yaml.safe_load(block) for block in re.findall(r"```ya?ml\s*\n(.*?)```", doc, re.S)]
    values = [example["approvers"] for example in examples
              if isinstance(example, dict) and "approvers" in example]
    assert values, "Keep a copyable approvers example in docs/gate.md"
    for logins in values:
        assert isinstance(logins, list) and logins
        assert all(str(login).casefold() != "jinyoon-builds" for login in logins), (
            "docs/gate.md still tells new users to copy the maintainer as their approver")
        assert all("<" in str(login) or "YOUR" in str(login).upper() for login in logins), (
            "Use an obvious user-login placeholder in the approvers example")
