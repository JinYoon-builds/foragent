"""Locked acceptance: fa init offers a project FA allow rule once, with consent.

Break-it: reject a missing/repeated offer, ignored acceptance, grants on decline,
broader/duplicate grants, and overwritten unrelated Claude settings.
"""

from copy import deepcopy
import json
from pathlib import Path
import re
import socket
import subprocess

import pytest
import yaml

from foragent import cli
from foragent.init import init_repo
from foragent.tracker import FakeTracker


RULE = "Bash(fa:*)"


class SetupGit:
    """The injected Git boundary never executes a process or writes history."""

    def __init__(self, root):
        self.root = root

    def check_worktree(self):
        pass

    def worktree_root(self):
        return self.root

    def remote_slug(self):
        return "example/project"

    def current_branch(self):
        return "main"

    def uncommitted_paths(self, paths):
        return []

    def has_unpushed_setup(self, branch):
        return False

    def commit_paths(self, *args, **kwargs):
        pass


@pytest.mark.parametrize("answer", ["yes", "no"], ids=["accept", "decline"])
@pytest.mark.parametrize("dry_run_first", [False, True], ids=["normal", "after-dry-run"])
def test_init_offers_once_and_only_accepted_rule_is_added(
    tmp_path, monkeypatch, answer, dry_run_first,
):
    home, root = tmp_path / "home", tmp_path / "project"
    home.mkdir()
    root.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(key, str(home / key.lower()))
    monkeypatch.setenv("FORAGENT_VIEW", "off")
    monkeypatch.setenv("FORAGENT_UPDATE_CHECK", "off")

    def forbidden(*args, **kwargs):
        pytest.fail(f"Init acceptance must use fake services: {args}")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(cli, "kick_tick", lambda *args: None)
    (root / ".foragent.yml").write_text(yaml.safe_dump({
        "repo": "example/project", "approvers": ["human"], "checks": "local",
        "executor": "claude", "language": "en", "skill_libraries": [],
    }), encoding="utf-8")
    paths = [root / ".claude/settings.json", root / ".claude/settings.local.json"]
    before = {}
    for index, path in enumerate(paths):
        path.parent.mkdir(exist_ok=True)
        value = {"permissions": {"allow": [f"Read(project-{index}/**)"],
                                 "deny": ["Read(.env)"], "ask": ["Bash(custom:*)"]},
                 "env": {"KEEP": str(index)}}
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        before[path] = deepcopy(value)
    before_bytes = {path: path.read_bytes() for path in paths}
    global_settings = home / ".claude/settings.json"
    global_settings.parent.mkdir(parents=True)
    global_settings.write_text('{"permissions":{"allow":["Read(personal/**)"]}}\n', encoding="utf-8")
    global_before = global_settings.read_bytes()
    tracker = FakeTracker(login="human")

    def gh_read(args, input=None):
        assert args == ["auth", "status"], args
        return ""

    def setup(root_arg, **kwargs):
        assert root_arg == root
        return init_repo(root, git=SetupGit(root), tracker=tracker, gh_run=gh_read, **kwargs)

    monkeypatch.setattr(cli, "init_repo", setup)
    offers = []

    def respond(prompt=""):
        if RULE in prompt:
            offers.append(prompt)
            return answer
        return "no"  # Decline unrelated optional offers.

    monkeypatch.setattr("builtins.input", respond)
    argv = ["init", "--repo-root", str(root), "--no-push", "--no-launchd", "--no-board",
            "--statusline", "no", "--starter", "no"]
    if dry_run_first:
        assert cli.main([*argv, "--dry-run"]) == 0
        assert offers == [], "A dry run must not consume the permission decision"
        assert {path: path.read_bytes() for path in paths} == before_bytes
    assert cli.main(argv) == 0
    assert len(offers) == 1, "fa init must propose the explicit Bash(fa:*) allow rule once"
    assert global_settings.read_bytes() == global_before, "The offer must not grant account-wide permissions"
    assert "project" in offers[0].lower(), "The offer must explain its project scope"
    after = {path: json.loads(path.read_text(encoding="utf-8")) for path in paths}
    grants = [rule for value in after.values() for rule in value.get("permissions", {}).get("allow", [])
              if rule not in {entry for original in before.values() for entry in original["permissions"]["allow"]}]
    assert grants == ([RULE] if answer == "yes" else []), "Consent grants exactly one FA rule"
    for path, original in before.items():
        value = deepcopy(after[path])
        value["permissions"]["allow"] = [item for item in value["permissions"]["allow"] if item != RULE]
        assert value == original, "Keep all unrelated Claude settings"
    if answer == "no":
        assert {path: path.read_bytes() for path in paths} == before_bytes
    after_bytes = {path: path.read_bytes() for path in paths}
    assert cli.main(argv) == 0
    assert len(offers) == 1, "Remember both acceptance and decline for this project"
    assert {path: path.read_bytes() for path in paths} == after_bytes, "Rerunning init must not rewrite the grant"
    assert global_settings.read_bytes() == global_before


def test_install_guidance_explains_the_offer_and_its_boundaries():
    root = next(path for path in Path(__file__).resolve().parents
                if (path / "pyproject.toml").is_file() and (path / "src/foragent").is_dir())
    readme = (root / "README.md").read_text(encoding="utf-8")
    skill = (root / "skills/fa-init/SKILL.md").read_text(encoding="utf-8")
    for source, text in (("installation docs", readme), ("init skill", skill)):
        assert RULE in text, f"{source}: explain the optional FA permission rule"
        assert re.search(r"accept|consent|approv|agree", text, re.I), f"{source}: require the person's acceptance"
    # Check the documented capability boundary, not production intent classifiers.
    flat = " ".join(readme.split())
    boundaries = {
        "permanent deletion": r"permanent\w*\s+(?:delet\w*|remov\w*)|(?:delet\w*|remov\w*).{0,30}permanent",
        "money": r"\bmoney\b|spend(?:ing)? money|financial transactions",
        "sending to others": r"send\w*.{0,40}(?:others|other people|third parties|external recipients)",
        "widening permissions": r"(?:widen\w*|expand\w*|broaden\w*|increas\w*).{0,30}(?:permissions?|privileges?)",
    }
    for name, pattern in boundaries.items():
        assert re.search(pattern, flat, re.I), f"Installation docs must explain the FA boundary: {name}"
