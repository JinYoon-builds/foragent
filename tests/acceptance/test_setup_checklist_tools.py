"""Acceptance for the first-run items of the setup checklist: tools installed and gh login.

The setup checklist (#1799) is one list that `fa doctor --setup --json`, `fa init` and /fa read:
{"items": [{"id", "ok", "fix"}]} in check order, `fix` present when an item is not ok. This issue
adds, in the list draft's order and before the gh project scope item:
git installed, gh installed, uv installed, gh logged in.

- An install item names the install command for this operating system (on macOS
  `brew install <tool>`; on Linux the distribution's command or the official install page).
  doctor only reports it; /fa runs it after the person agrees.
- The gh login item's fix is the person's own `gh auth login`; FA never touches credentials.
- With gh missing, the items that need gh (login, project scope) say they wait on gh instead of
  failing with an auth command that cannot work yet.

Item ids are the lane's choice. The tests find each item by what changes when one tool goes
missing or gh is logged out. Only fake tools are on PATH; each one logs its calls, so the tests
prove that doctor installs nothing and never runs a login.
"""

import json
import os
from pathlib import Path
import shutil
import sys

import pytest

from foragent import cli

from conftest import GitHistory, git_command

SCOPE_ITEM = "gh-project-scope"

FAKE_GH = r'''
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_TOOL_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({"tool": "gh", "args": args}) + "\n")
logged_out = os.environ.get("FAKE_GH_LOGGED_OUT") == "1"
if args[:1] == ["--version"] or args[:1] == ["version"]:
    print("gh version 2.62.0 (2024-11-14)")
elif args[:2] == ["auth", "status"]:
    if logged_out:
        print("You are not logged into any GitHub hosts. To log in, run: gh auth login", file=sys.stderr)
        sys.exit(1)
    print("github.com")
    print("  ✓ Logged in to github.com account human (keyring)")
    print("  - Active account: true")
    print("  - Git operations protocol: https")
    print("  - Token: gho_************************************")
    print("  - Token scopes: 'gist', 'project', 'read:org', 'repo', 'workflow'")
elif args[:2] == ["auth", "token"]:
    if logged_out:
        print("no oauth token found for github.com", file=sys.stderr)
        sys.exit(1)
    print("gho_offline_token")
elif args[:2] == ["repo", "view"]:
    print(json.dumps({"isPrivate": True}))
elif args[:2] == ["api", "user"]:
    print(json.dumps({"login": "human", "type": "User"}))
else:
    print("[]")
'''

# uv, brew and the Linux package managers: log the call, print a version, never install anything.
FAKE_LOGGING_TOOL = r'''
import json, os, sys
with open(os.environ["FAKE_TOOL_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({"tool": os.path.basename(sys.argv[0]), "args": sys.argv[1:]}) + "\n")
print("1.0.0")
'''

FAKE_AGENT = r'''
import sys
args = sys.argv[1:]
if args[:2] == ["auth", "status"]:
    print('{"loggedIn": true}')
elif args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT")
'''

INSTALLERS = ("brew", "apt", "apt-get", "dnf", "yum", "pacman", "zypper", "apk", "port")


@pytest.fixture
def machine(tmp_path, monkeypatch):
    for key, value in (("GIT_TERMINAL_PROMPT", "0"), ("FORAGENT_UPDATE_CHECK", "off"),
                       ("FORAGENT_ORCH_SPAWN", "off"),
                       ("GIT_AUTHOR_NAME", "Test Human"), ("GIT_AUTHOR_EMAIL", "human@example.com"),
                       ("GIT_COMMITTER_NAME", "Test Human"), ("GIT_COMMITTER_EMAIL", "human@example.com")):
        monkeypatch.setenv(key, value)
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "ANTHROPIC_API_KEY",
                 "CLAUDE_CODE_OAUTH_TOKEN", "FAKE_GH_LOGGED_OUT"):
        monkeypatch.delenv(name, raising=False)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "git").symlink_to(shutil.which("git"))
    scripts = {"gh": FAKE_GH, "claude": FAKE_AGENT, "codex": FAKE_AGENT, "uv": FAKE_LOGGING_TOOL,
               **{name: FAKE_LOGGING_TOOL for name in INSTALLERS}}
    for name, source in scripts.items():
        tool = bin_dir / name
        tool.write_text(f"#!{sys.executable}\n{source}", encoding="utf-8")
        tool.chmod(0o755)
    log = tmp_path / "tool-calls.jsonl"
    monkeypatch.setenv("FAKE_TOOL_LOG", str(log))
    # Only the fake bin folder: /usr/bin holds a real git on macOS and Linux.
    monkeypatch.setenv("PATH", str(bin_dir))

    origin, repo = tmp_path / "origin.git", tmp_path / "project"
    git_command(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    GitHistory(origin, repo).update({"README.md": "# Demo\n"})
    git_command(tmp_path, "clone", "-q", str(origin), str(repo))
    git_command(repo, "remote", "set-url", "origin", "https://github.com/owner/repo.git")

    class Machine:
        root = repo
        home = Path(os.environ["HOME"])

        @staticmethod
        def remove(tool: str) -> None:
            (bin_dir / tool).rename(tmp_path / f"{tool}.off")

        @staticmethod
        def restore(tool: str) -> None:
            (tmp_path / f"{tool}.off").rename(bin_dir / tool)

        @staticmethod
        def log_out() -> None:
            monkeypatch.setenv("FAKE_GH_LOGGED_OUT", "1")

        @staticmethod
        def calls() -> list[dict]:
            if not log.is_file():
                return []
            return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]

    return Machine


def doctor_items(machine, capsys) -> tuple[int, list[dict]]:
    capsys.readouterr()
    try:
        code = cli.main(["doctor", "--setup", "--json", "--repo-root", str(machine.root)])
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    assert "Traceback" not in captured.out + captured.err
    try:
        report = json.loads(captured.out)
    except json.JSONDecodeError:
        pytest.fail(f"`fa doctor --setup --json` must print one JSON object: {captured.out!r} {captured.err!r}")
    items = report.get("items") if isinstance(report, dict) else None
    assert isinstance(items, list) and items, f"The checklist needs a non-empty items list: {report!r}"
    for item in items:
        assert isinstance(item.get("id"), str) and item["id"], f"Every item needs an id: {item!r}"
        assert isinstance(item.get("ok"), bool), f"Every item needs ok as a bool: {item!r}"
        if not item["ok"]:
            assert isinstance(item.get("fix"), str) and item["fix"].strip(), f"A failing item names its fix: {item!r}"
    ids = [item["id"] for item in items]
    assert len(ids) == len(set(ids)), f"Item ids must be unique: {ids}"
    return code, items


def flipped(before: list[dict], after: list[dict]) -> list[dict]:
    """Items, in check order, that passed on the full machine and fail now."""
    passed = {item["id"] for item in before if item["ok"]}
    return [item for item in after if item["id"] in passed and not item["ok"]]


def first_flipped(before: list[dict], after: list[dict], why: str) -> dict:
    changed = flipped(before, after)
    assert changed, f"{why}: an item must fail: {after!r}"
    return changed[0]


def by_id(items: list[dict], item_id: str) -> dict:
    found = [item for item in items if item["id"] == item_id]
    assert len(found) == 1, f"The checklist must hold one {item_id} item: {items!r}"
    return found[0]


def assert_install_fix(fix: str, tool: str) -> None:
    if sys.platform == "darwin":
        commands = [f"brew install {tool}"] + (["xcode-select --install"] if tool == "git" else [])
        assert any(command in fix for command in commands), f"On macOS the {tool} fix is {commands}: {fix!r}"
    else:
        assert tool in fix and ("install" in fix or "https://" in fix), \
            f"On this OS the {tool} fix names the install command or the official page: {fix!r}"
        assert "brew" not in fix, f"brew is the macOS command, not this OS's: {fix!r}"


def assert_nothing_installed_or_logged_in(calls: list[dict]) -> None:
    installs = [call for call in calls if call["tool"] in INSTALLERS]
    assert installs == [], f"doctor only reports; /fa installs after the person agrees: {installs}"
    auth = [call for call in calls if call["tool"] == "gh" and call["args"][:1] == ["auth"]
            and call["args"][1:2] not in (["status"], ["token"])]
    assert auth == [], f"FA never runs a login or changes credentials: {auth}"


@pytest.mark.parametrize("tool", ["git", "gh", "uv"])
def test_a_missing_tool_fails_one_install_item_with_the_command_for_this_os(machine, capsys, tool):
    _code, full = doctor_items(machine, capsys)
    machine.remove(tool)

    code, items = doctor_items(machine, capsys)

    changed = flipped(full, items)
    assert changed, f"Without {tool} on PATH an item must fail: {items!r}"
    install = changed[0]
    assert_install_fix(install["fix"], tool)
    if tool == "uv":
        assert [item["id"] for item in changed] == [install["id"]], f"Only the uv item depends on uv: {changed!r}"
    machine.restore(tool)
    _code, again = doctor_items(machine, capsys)
    assert by_id(again, install["id"])["ok"] is True, f"With {tool} back the item passes: {again!r}"
    assert code != 0, "doctor exits non-zero when an item fails"
    assert_nothing_installed_or_logged_in(machine.calls())


def test_without_gh_the_login_and_scope_items_wait_on_gh_instead_of_asking_for_auth(machine, capsys):
    _code, full = doctor_items(machine, capsys)
    machine.log_out()
    _code, logged_out = doctor_items(machine, capsys)
    login = [item for item in flipped(full, logged_out) if "gh auth login" in item["fix"]]
    assert len(login) == 1, f"Exactly one item asks the person to log in: {logged_out!r}"
    machine.remove("gh")

    _code, items = doctor_items(machine, capsys)

    install = first_flipped(full, items, "Without gh")
    assert_install_fix(install["fix"], "gh")
    for waiting in (by_id(items, login[0]["id"]), by_id(items, SCOPE_ITEM)):
        assert waiting["ok"] is False, f"An item that needs gh cannot pass without gh: {waiting!r}"
        assert "gh auth" not in waiting["fix"], f"Without gh, an auth command is a misleading fix: {waiting!r}"
        text = json.dumps(waiting)
        assert install["id"] in text or install["fix"] in text, \
            f"The item says it waits on the gh install item: {waiting!r}"
    assert_nothing_installed_or_logged_in(machine.calls())


def test_logged_out_gh_fails_the_login_item_with_the_persons_own_gh_auth_login(machine, capsys):
    _code, full = doctor_items(machine, capsys)
    before = sorted(p.name for p in machine.home.rglob("*")), sorted(p.name for p in machine.root.rglob("*"))
    machine.log_out()

    code, items = doctor_items(machine, capsys)

    login = [item for item in flipped(full, items) if "gh auth login" in item["fix"]]
    assert len(login) == 1, f"Exactly one item asks the person to log in: {items!r}"
    fix = login[0]["fix"]
    assert fix.lstrip("! ").startswith("gh auth login"), f"The fix is the person's own gh auth login: {fix!r}"
    assert "token" not in fix.casefold(), f"FA never asks for or passes a token: {fix!r}"
    assert code != 0
    assert (sorted(p.name for p in machine.home.rglob("*")), sorted(p.name for p in machine.root.rglob("*"))) == before
    assert_nothing_installed_or_logged_in(machine.calls())


def test_tool_and_login_items_come_first_in_the_list_drafts_order(machine, capsys):
    """git, gh, uv installed, then gh logged in, all before the gh project scope item."""
    _code, full = doctor_items(machine, capsys)
    found = {}
    for tool in ("git", "gh", "uv"):
        machine.remove(tool)
        _code, items = doctor_items(machine, capsys)
        found[tool] = first_flipped(full, items, f"Without {tool}")["id"]
        machine.restore(tool)
    machine.log_out()
    _code, items = doctor_items(machine, capsys)
    login = [item for item in flipped(full, items) if "gh auth login" in item["fix"]]
    assert len(login) == 1, f"Exactly one item asks the person to log in: {items!r}"
    found["login"] = login[0]["id"]

    order = [item["id"] for item in full]
    expected = [found["git"], found["gh"], found["uv"], found["login"], SCOPE_ITEM]
    assert len(set(expected)) == 5, f"Five distinct items: {found!r}"
    for item_id in expected[:4]:
        assert by_id(full, item_id)["ok"] is True, f"On a full machine the item passes: {full!r}"
    assert [order.index(item_id) for item_id in expected] == sorted(order.index(item_id) for item_id in expected), \
        f"Order: git, gh, uv installed, gh login, gh project scope: {order}"


def test_fa_skill_walks_the_setup_checklist():
    skill = "\n".join(path.read_text(encoding="utf-8")
                      for path in sorted((Path(__file__).resolve().parents[2] / "skills" / "fa").rglob("*.md")))
    assert "fa doctor --setup --json" in skill, "/fa reads the setup checklist from `fa doctor --setup --json`"
