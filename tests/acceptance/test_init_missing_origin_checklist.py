"""Acceptance (n80): `fa init` shows the whole setup checklist when origin is missing.

Setup under test: a Git worktree, git installed, no origin remote, neither gh nor uv on PATH.
Today init stops on the missing origin before it collects the checklist, and the first
repository suggestion is `gh repo create`, which cannot run without gh.

Contract locked here (WHAT only):
- init exits nonzero and shows the repository-linked, git-installed, gh-installed,
  uv-installed, gh-logged-in and gh-project-scope entries together. git-installed shows as
  satisfied; gh-logged-in and gh-project-scope show that they wait on a prerequisite.
- The repository-linked (origin) entry comes before the tool and authentication entries.
- The first recovery command in the output runs with the installed git (adding origin),
  not with the missing gh.
- Nothing is created: no remote, no setup files.

Item ids are the checklist ids shared with `fa doctor --setup`. An entry's line is the first
output line whose first-named item id is that id (so "gh-logged-in: Complete gh-installed
first." is the gh-logged-in entry). The exact wording and status marks are the lane's.

Reuses the fake-tool machine of the setup checklist locks: only fake tools and a real git
are on PATH, HOME is a temp folder, nothing reaches GitHub.
"""

import re
import sys

import pytest

from test_setup_checklist_tools import machine  # noqa: F401 -- fixture

from conftest import git_command
from foragent import cli
from foragent.setup import INSTALL_PAGES

IDS = ("repository-linked", "git-installed", "gh-installed", "uv-installed",
       "gh-logged-in", "gh-project-scope")
TOOL_AND_AUTH = IDS[1:]
WAITING = ("gh-logged-in", "gh-project-scope")
HANGUL = re.compile(r"[가-힣]")
# Words that say an entry is satisfied, in either language.
SATISFIED = re.compile(r"(?<![\w-])(PASS|OK|ok|satisfied|done)(?![\w-])|✓|✔|완료|통과|충족|설치됨")
FAILED = re.compile(r"(?<![\w-])FAIL(?![\w-])|✗|✘|실패")
# A command the person could run: a tool name followed by a subcommand word.
COMMAND = re.compile(r"(?<![\w/.-])(gh|git|brew|uv|curl)[ \t]+[a-z][\w-]*")
INIT_FLAGS = ["--no-push", "--no-launchd", "--no-board", "--statusline", "no"]


def id_pattern(item_id: str) -> re.Pattern:
    return re.compile(rf"(?<![\w-]){re.escape(item_id)}(?![\w-])")


def files(root) -> dict:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and ".git" not in p.relative_to(root).parts}


@pytest.fixture
def no_origin(machine):  # noqa: F811
    machine.remove("gh")
    machine.remove("uv")
    git_command(machine.root, "remote", "remove", "origin")
    return machine


def run_init(machine, capsys, language: str) -> tuple[int, str, list[str]]:
    before = files(machine.root), files(machine.home)
    capsys.readouterr()
    try:
        code = cli.main(["init", "--repo-root", str(machine.root), "--language", language, *INIT_FLAGS])
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "Traceback" not in output, output
    assert (files(machine.root), files(machine.home)) == before, \
        f"init creates no setup files while it stops:\n{output}"
    remotes = git_command(machine.root, "remote")
    assert not (remotes or "").strip(), f"init links no remote: {remotes!r}\n{output}"
    assert not (machine.root / ".foragent.yml").exists()
    calls = [call for call in machine.calls() if call["tool"] != "git"]
    assert [call for call in calls if call["tool"] in ("gh", "uv", "brew")] == [], \
        f"gh and uv are missing and nothing is installed: {calls}"
    return code, output, [line for line in output.splitlines() if line.strip()]


def entries(output_lines: list[str]) -> dict[str, int]:
    """Each item id -> index of its entry line (the one line that names it first)."""
    found: dict[str, list[int]] = {}
    for index, line in enumerate(output_lines):
        named = [(match.start(), item_id) for item_id in IDS
                 for match in [id_pattern(item_id).search(line)] if match]
        if named:
            found.setdefault(min(named)[1], []).append(index)
    repeated = {item_id: places for item_id, places in found.items() if len(places) > 1}
    assert not repeated, f"Each checklist entry is shown once: {repeated}\n" + "\n".join(output_lines)
    return {item_id: places[0] for item_id, places in found.items()}


@pytest.mark.parametrize("language", ["en", "ko"])
def test_missing_origin_shows_every_checklist_entry_with_its_status(no_origin, capsys, language):
    code, output, shown = run_init(no_origin, capsys, language)

    assert code != 0, f"init stops on an incomplete setup:\n{output}"
    found = entries(shown)
    missing = [item_id for item_id in IDS if item_id not in found]
    assert not missing, f"init shows every checklist entry together; missing {missing}:\n{output}"

    git_line = shown[found["git-installed"]]
    assert SATISFIED.search(git_line) and not FAILED.search(git_line), \
        f"git is installed: its entry shows a satisfied status: {git_line!r}\n{output}"
    assert "install git" not in git_line and "git-scm.com" not in git_line, \
        f"A satisfied git entry offers no install fix: {git_line!r}"
    for tool in ("gh", "uv"):
        line = shown[found[f"{tool}-installed"]]
        fix = f"brew install {tool}" if sys.platform == "darwin" else INSTALL_PAGES[tool]
        assert fix in line, f"The missing {tool} entry names its install step {fix!r}: {line!r}\n{output}"
        assert not SATISFIED.search(line), f"The missing {tool} is not shown as satisfied: {line!r}\n{output}"
    for item_id in WAITING:
        line = shown[found[item_id]]
        # The entry's own id does not count as naming its prerequisite.
        prerequisites = [other for other in ("gh-installed", "gh-logged-in") if other != item_id]
        assert any(id_pattern(other).search(line) for other in prerequisites), \
            f"{item_id} says which prerequisite it waits on: {line!r}\n{output}"
        assert "gh auth" not in line, f"Without gh an auth command cannot run: {line!r}"
    if language == "ko":
        assert HANGUL.search(output), f"Korean under --language ko:\n{output}"
    else:
        assert not HANGUL.search(output), f"English under --language en:\n{output}"


@pytest.mark.parametrize("language", ["en", "ko"])
def test_origin_entry_comes_first_and_its_first_command_uses_git(no_origin, capsys, language):
    code, output, shown = run_init(no_origin, capsys, language)

    assert code != 0, output
    found = entries(shown)
    assert "repository-linked" in found, f"init shows the origin entry:\n{output}"
    later = {item_id: found.get(item_id) for item_id in TOOL_AND_AUTH}
    assert all(index is not None for index in later.values()), \
        f"init shows the tool and login entries with the origin entry: {later}\n{output}"
    assert all(found["repository-linked"] < index for index in later.values()), \
        f"The origin entry comes before the tool and login entries: {found}\n{output}"

    commands = [match.group(0) for line in shown for match in COMMAND.finditer(line)]
    assert commands, f"init suggests a recovery command:\n{output}"
    first = next(i for i, line in enumerate(shown) if COMMAND.search(line))
    assert first >= found["repository-linked"], \
        f"The first command belongs to the origin entry or later:\n{output}"
    assert commands[0].startswith("git remote"), \
        f"The first recovery command runs with the installed git, not the missing gh: {commands[0]!r}\n{output}"
    line = shown[first]
    assert "git remote add origin" in line, \
        f"The first command adds the missing origin: {line!r}\n{output}"
