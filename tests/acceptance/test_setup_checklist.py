"""Acceptance for the setup checklist skeleton (r403) and its first item, the gh project scope (r402).

One checklist, several readers. `fa doctor --setup --json` prints it for machines and for /fa;
`fa init` reads the same list and shows the same fixes. The first real item checks that the
gh token can write GitHub Projects: the default `gh auth login` token has no `project` scope.

Contract for later checklist items (declared here, read by /fa):
- `fa doctor --setup --json` prints one JSON object on stdout. Its "items" key is a list in
  check order. Each item has "id" (a stable string), "ok" (a bool) and, when not ok, "fix"
  (the exact command or settings pane the person uses). Other keys are free.
- It works before `fa init` (no .foragent.yml needed) and it writes nothing.
- The gh project scope item has the id "gh-project-scope" and the fix
  `gh auth refresh -h github.com -s project`.
- `fa init` prints the fix of every item that doctor reports not ok.

Only git and fake `gh`, `claude` and `codex` executables are on PATH. The fake gh logs every
call, so the tests can prove that FA never changes credentials and never writes to GitHub
before the scope is fixed.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from foragent import cli

from conftest import GitHistory, git_command

FIX = "gh auth refresh -h github.com -s project"
ITEM = "gh-project-scope"

FAKE_GH = r'''
import json, os, sys
args = sys.argv[1:]
stdin = "" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.read()
with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({"args": args, "stdin": stdin}) + "\n")
if args[:2] == ["auth", "status"]:
    scopes = ", ".join(f"'{name}'" for name in os.environ["FAKE_GH_SCOPES"].split(","))
    print("github.com")
    print("  ✓ Logged in to github.com account human (keyring)")
    print("  - Active account: true")
    print("  - Git operations protocol: https")
    print("  - Token: gho_************************************")
    print(f"  - Token scopes: {scopes}")
elif args[:2] == ["auth", "token"]:
    print("gho_offline_token")
elif args[:2] == ["repo", "view"]:
    print(json.dumps({"isPrivate": True}))
elif args[:2] == ["api", "user"]:
    print(json.dumps({"login": "human", "type": "User"}))
else:
    print("[]")
'''

FAKE_TOOL = r'''
import sys
args = sys.argv[1:]
if args[:2] == ["auth", "status"]:
    print('{"loggedIn": true}')
elif args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT")
'''

DEFAULT_SCOPES = "gist,read:org,repo,workflow"


@pytest.fixture
def machine(tmp_path, monkeypatch):
    for key, value in (("GIT_TERMINAL_PROMPT", "0"), ("FORAGENT_UPDATE_CHECK", "off"),
                       ("FORAGENT_ORCH_SPAWN", "off"),
                       ("GIT_AUTHOR_NAME", "Test Human"), ("GIT_AUTHOR_EMAIL", "human@example.com"),
                       ("GIT_COMMITTER_NAME", "Test Human"), ("GIT_COMMITTER_EMAIL", "human@example.com")):
        monkeypatch.setenv(key, value)
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "ANTHROPIC_API_KEY",
                 "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "git").symlink_to(shutil.which("git"))
    for name, source in (("gh", FAKE_GH), ("claude", FAKE_TOOL), ("codex", FAKE_TOOL)):
        tool = bin_dir / name
        tool.write_text(f"#!{sys.executable}\n{source}", encoding="utf-8")
        tool.chmod(0o755)
    log = tmp_path / "gh-calls.jsonl"
    monkeypatch.setenv("FAKE_GH_LOG", str(log))
    monkeypatch.setenv("FAKE_GH_SCOPES", DEFAULT_SCOPES)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + "/usr/bin" + os.pathsep + "/bin")

    origin, repo = tmp_path / "origin.git", tmp_path / "project"
    git_command(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    GitHistory(origin, repo).update({"README.md": "# Demo\n"})
    git_command(tmp_path, "clone", "-q", str(origin), str(repo))
    # A GitHub origin, so the repository is linked; nothing in these tests pushes to it.
    git_command(repo, "remote", "set-url", "origin", "https://github.com/owner/repo.git")

    class Machine:
        root = repo
        home = Path(os.environ["HOME"])

        @staticmethod
        def scopes(value: str) -> None:
            monkeypatch.setenv("FAKE_GH_SCOPES", value)

        @staticmethod
        def gh_calls() -> list[dict]:
            if not log.is_file():
                return []
            return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]

        @staticmethod
        def clear() -> None:
            log.write_text("", encoding="utf-8")

    return Machine


def run(argv, capsys) -> tuple[int, str, str]:
    capsys.readouterr()
    try:
        code = cli.main(argv)
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def doctor_items(machine, capsys) -> tuple[int, list[dict]]:
    code, out, err = run(["doctor", "--setup", "--json", "--repo-root", str(machine.root)], capsys)
    assert "Traceback" not in out + err
    try:
        report = json.loads(out)
    except json.JSONDecodeError:
        pytest.fail(f"`fa doctor --setup --json` must print one JSON object on stdout: {out!r} {err!r}")
    items = report.get("items") if isinstance(report, dict) else None
    assert isinstance(items, list) and items, f"The checklist report needs a non-empty items list: {report!r}"
    for item in items:
        assert isinstance(item.get("id"), str) and item["id"], f"Every item needs a stable id: {item!r}"
        assert isinstance(item.get("ok"), bool), f"Every item needs ok as a bool: {item!r}"
        if not item["ok"]:
            assert isinstance(item.get("fix"), str) and item["fix"].strip(), f"A failing item names its fix: {item!r}"
    ids = [item["id"] for item in items]
    assert len(ids) == len(set(ids)), f"Item ids must be unique: {ids}"
    return code, items


def scope_item(items: list[dict]) -> dict:
    found = [item for item in items if item["id"] == ITEM]
    assert len(found) == 1, f"The checklist must hold one {ITEM} item: {items!r}"
    return found[0]


def tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and ".git" not in p.relative_to(root).parts}


def github_writes(calls: list[dict]) -> list[dict]:
    """Every gh call that could change GitHub or the person's credentials."""
    writes = []
    for call in calls:
        args = call["args"]
        method = next((args[i + 1] for i, arg in enumerate(args[:-1]) if arg in {"-X", "--method"}), "GET")
        if (args[:1] == ["auth"] and args[1:2] != ["status"] and args[1:2] != ["token"]) \
                or method.upper() != "GET" \
                or any(arg in {"-f", "-F", "--field", "--raw-field"} for arg in args) \
                or "mutation" in call["stdin"] \
                or args[:1] in (["label"], ["project"]) \
                or args[:2] in (["repo", "create"], ["issue", "create"], ["issue", "edit"]):
            writes.append(call)
    return writes


def test_doctor_setup_reports_missing_project_scope_with_the_exact_fix(machine, capsys):
    before = tree(machine.root), tree(machine.home)

    code, items = doctor_items(machine, capsys)

    item = scope_item(items)
    assert item["ok"] is False, f"A token without the project scope fails the item: {item!r}"
    assert item["fix"] == FIX, f"The fix is the exact command the person runs: {item!r}"
    assert code != 0, "doctor exits non-zero when an item fails"
    assert not (machine.root / ".foragent.yml").exists()
    assert (tree(machine.root), tree(machine.home)) == before, "doctor --setup only reads"
    assert github_writes(machine.gh_calls()) == [], "doctor never changes GitHub or credentials"


@pytest.mark.parametrize("scopes", ["gist,project,read:org,repo,workflow", "repo,read:org,project"])
def test_doctor_setup_passes_the_scope_item_when_project_is_granted(machine, capsys, scopes):
    machine.scopes(scopes)

    _code, items = doctor_items(machine, capsys)

    item = scope_item(items)
    assert item["ok"] is True, f"A token with the project scope passes the item: {item!r}"
    assert github_writes(machine.gh_calls()) == []


def test_init_stops_before_any_github_write_and_shows_the_doctor_fixes(machine, capsys):
    _code, items = doctor_items(machine, capsys)
    failing = [item for item in items if not item["ok"]]
    assert scope_item(items) in failing
    machine.clear()
    before = tree(machine.root), tree(machine.home)
    head = git_command(machine.root, "rev-parse", "HEAD")

    code, out, err = run(["init", "--repo-root", str(machine.root), "--approver", "human", "--language", "en",
                          "--no-push", "--no-launchd", "--statusline", "no"], capsys)

    output = out + err
    assert code != 0, f"init stops while the token cannot write the board: {output!r}"
    assert "Traceback" not in output
    for item in failing:
        assert item["fix"] in output, f"init shows the same fix as doctor for {item['id']}: {output!r}"
    calls = machine.gh_calls()
    assert github_writes(calls) == [], f"FA never writes to GitHub or touches credentials here: {calls}"
    assert not any(call["args"][:2] == ["api", "graphql"] for call in calls), "No board call before the fix"
    assert not (machine.root / ".foragent.yml").exists(), "init stops before it sets the project up"
    assert (tree(machine.root), tree(machine.home)) == before
    assert git_command(machine.root, "rev-parse", "HEAD") == head


def test_init_and_doctor_agree_once_the_scope_is_granted(machine, capsys):
    machine.scopes("gist,project,read:org,repo,workflow")
    _code, items = doctor_items(machine, capsys)
    assert scope_item(items)["ok"] is True

    code, out, err = run(["init", "--repo-root", str(machine.root), "--approver", "human", "--language", "en",
                          "--no-push", "--no-launchd", "--statusline", "no", "--dry-run"], capsys)

    output = out + err
    assert "Traceback" not in output
    assert FIX not in output, f"init must not ask for a scope the token already has: {output!r}"
    for item in items:
        if not item["ok"]:
            assert item["fix"] in output, f"init shows the same fix as doctor for {item['id']}: {output!r}"
    if all(item["ok"] for item in items):
        assert code == 0, f"A machine that passes the checklist can run init: {output!r}"
    assert github_writes(machine.gh_calls()) == []
