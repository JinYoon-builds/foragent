"""Acceptance: `fa init` goes on when a default skill name matches skills in several libraries.

UX research (r807, r814, r815; three sessions) found that when two skill libraries both ship a
skill with the same name (pstack and pstack-skills, both with principle-prove-it-works), `fa init`
stops with rc=1 ("ambiguous skill: ...; use one of: ...") and names no flag, file or setting, so
the person cannot finish setup.

Jin's decision (q24, 2026-11-06): setup continues. For every bare default skill name that matches
more than one skill, init picks one by a fixed rule, the first of the qualified ids in name order,
and prints one line that names the picked id and the command to change it (`fa lanes skills`).
English and Korean. A name that matches exactly one skill behaves as before: it is used and no
such line is printed.

Contract locked here (WHAT only, D98): the exit code, which copy is installed, and the count and
content of the lines that name `fa lanes skills`. The wording around the id and the command,
and whether the line also lists the other candidates, are the lane's.

Fixture: the fake-tool machine of the setup checklist locks (temp HOME, fake gh/claude/codex,
nothing reaches GitHub) plus two local skill libraries in the temp HOME's global config. The
vendored pstack library also ships both default names, so each name has several candidates:
- principle-prove-it-works: alpha-skills, pstack, zeta-skills -> alpha-skills is first.
- principle-attack-the-premise: pstack, zeta-skills -> pstack is first.
- principle-fix-root-causes: pstack only -> used without a pick line.
"""

import re

import yaml

from test_setup_checklist_tools import machine  # noqa: F401 -- fixture

from foragent import cli
from foragent.git import GitRepo

HANGUL = re.compile(r"[가-힣]")
COMMAND = "fa lanes skills"
INIT_FLAGS = ["--no-push", "--no-launchd", "--no-board", "--statusline", "no"]
MIT = """MIT License

Copyright (c) 2026 Test Author

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
PROVE = "principle-prove-it-works"
ATTACK = "principle-attack-the-premise"
UNIQUE = "principle-fix-root-causes"
LIBRARIES = {"zeta-skills": (PROVE, ATTACK), "alpha-skills": (PROVE,)}
# The first qualified id in name order for each ambiguous default name.
PICKED = {PROVE: "alpha-skills:" + PROVE, ATTACK: "pstack:" + ATTACK}


def add_libraries(machine, tmp_path):
    """Two local libraries in the person's global config; zeta is listed first on purpose."""
    entries = []
    for name, skills in LIBRARIES.items():
        library = tmp_path / "libraries" / name
        for skill in skills:
            folder = library / "skills" / skill
            folder.mkdir(parents=True)
            (folder / "SKILL.md").write_text(
                f"---\nname: {skill}\ndescription: {skill} from {name}\n---\nCopy from {name}.\n",
                encoding="utf-8")
        (library / "LICENSE").write_text(MIT, encoding="utf-8")
        entries.append({"name": name, "repo": str(library), "path": str(library)})
    config = machine.home / ".config/foragent/config.yml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(yaml.safe_dump({"skill_libraries": entries}), encoding="utf-8")


def run(argv, capsys):
    capsys.readouterr()
    try:
        code = cli.main(argv)
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def pick_lines(output):
    return [line.strip() for line in output.splitlines() if COMMAND in line]


def assert_picks(code, output):
    assert "Traceback" not in output, output
    assert code == 0, f"init goes on when a default skill name matches several skills:\n{output}"
    lines = pick_lines(output)
    assert len(lines) == len(PICKED), (
        f"One line per ambiguous default name names {COMMAND!r}; a unique name gets none: {lines}\n{output}")
    for name, picked in PICKED.items():
        naming = [line for line in lines if picked in line]
        assert len(naming) == 1, f"Exactly one line names the pick {picked} and {COMMAND!r}: {lines}\n{output}"
        assert name in naming[0]
    assert not any(UNIQUE in line for line in lines), (
        f"{UNIQUE} matches one skill: it is used without a pick line: {lines}")
    return lines


def test_init_in_english_picks_the_first_id_installs_it_and_says_how_to_change_it(machine, capsys, tmp_path,  # noqa: F811
                                                                                   monkeypatch):  # noqa: F811
    add_libraries(machine, tmp_path)
    # The suite forbids real commits; setup's commit is the same stand-in the default-skill locks use.
    monkeypatch.setattr(GitRepo, "commit_paths", lambda *args, **kwargs: None)

    code, output = run(["init", "--repo-root", str(machine.root), "--language", "en", *INIT_FLAGS], capsys)

    lines = assert_picks(code, output)
    assert not HANGUL.search(" ".join(lines)), f"English pick lines under --language en: {lines}"
    installed = machine.root / ".claude/skills"
    prove = (installed / PROVE / "SKILL.md").read_text(encoding="utf-8")
    assert "Copy from alpha-skills." in prove, f"The picked alpha-skills copy is installed:\n{prove}"
    attack = (installed / ATTACK / "SKILL.md").read_text(encoding="utf-8")
    assert "Copy from zeta-skills." not in attack, f"The picked pstack copy is installed, not zeta-skills:\n{attack}"
    assert (installed / UNIQUE / "SKILL.md").is_file(), "The unique default is installed as before"


def test_init_dry_run_in_korean_picks_the_first_id_and_says_how_to_change_it(machine, capsys, tmp_path):  # noqa: F811
    add_libraries(machine, tmp_path)

    code, output = run(["init", "--repo-root", str(machine.root), "--language", "ko", "--dry-run", *INIT_FLAGS],
                       capsys)

    lines = assert_picks(code, output)
    for line in lines:
        assert HANGUL.search(line), f"Korean pick line under --language ko: {line!r}"
    assert not (machine.root / ".claude/skills").exists(), "A dry run installs nothing"
