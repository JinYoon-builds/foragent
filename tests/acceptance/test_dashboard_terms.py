"""Acceptance contract: the terminal UI is called the dashboard in what people read.

Jin decided on 2026-11-04 that names follow common IT terms. The live terminal UI
that `fa gate watch` draws is "the dashboard" (Korean: 대시보드). The old names
"TUI", "lane view" and "레인 화면" must not name it in CLI help, in the messages
catalogue, in the README, in the docs pages a person reads, or in the skills
that speak to the person. The squirrel world stays in the pictures only.

History is out of scope: docs/decisions.md and the archive folders under docs/
(design, research, impl, specs) keep their words. Code identifiers, module
names and the `fa gate watch` command name are not people-facing text here.
"""

from dataclasses import replace
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

from test_view_open_terminals import terminal_case


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src"
ARCHIVES = ("decisions.md", "design", "research", "impl", "specs")

# Old names for Foragent's terminal UI. "TUI" is matched as a whole word in
# capitals; the English and Korean UI names in any case and spacing.
OLD_NAMES = re.compile(r"\bTUI\b|\blane[ -]views?\b|레인\s*화면|레인\s*뷰", re.IGNORECASE)
OLD_UI_NAMES = re.compile(r"\blane[ -]views?\b|레인\s*화면|레인\s*뷰", re.IGNORECASE)
NEW_NAME = re.compile(r"dashboard|대시보드", re.IGNORECASE)


def old_names(text, pattern=OLD_NAMES):
    return sorted({match.group(0) for match in pattern.finditer(text)})


def fa_help(*args):
    result = subprocess.run(
        [sys.executable, "-m", "foragent.cli", *args, "--help"], cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(SOURCE)}, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout + result.stderr


def help_text(capsys, *args):
    from foragent import cli

    with pytest.raises(SystemExit) as exit_:
        cli.main([*args, "--help"])
    assert exit_.value.code == 0, args
    output = capsys.readouterr()
    return output.out + output.err


def test_help_names_the_dashboard_and_keeps_gate_watch(capsys):
    top = fa_help()
    commands = sorted({match.group(1) for match in re.finditer(r"^  ([a-z][a-z-]*)\s", top, re.M)})
    assert {"gate", "init", "design"} <= set(commands), commands
    # `fa test` waits for a machine-wide test slot before it reads --help.
    outputs = {"fa --help": top}
    for command in commands:
        if command != "test":
            outputs[f"fa {command} --help"] = help_text(capsys, command)
    outputs["fa gate watch --help"] = help_text(capsys, "gate", "watch")
    stale = {command: old_names(output) for command, output in outputs.items() if old_names(output)}
    assert not stale, f"Help still names the UI the old way: {stale}"
    watch_lines = [line for line in outputs["fa gate --help"].splitlines()
                   if line.split()[:1] == ["watch"]]
    assert watch_lines, "`fa gate watch` must stay listed for scripts"
    assert "dashboard" in " ".join(watch_lines).lower(), (
        f"The gate help must say that watch shows the dashboard: {watch_lines}")
    assert "dashboard" in outputs["fa gate watch --help"].lower(), outputs["fa gate watch --help"]


@pytest.mark.parametrize("language,name", [("en", "dashboard"), ("ko", "대시보드")])
def test_messages_catalogue_names_the_dashboard(language, name):
    from foragent import i18n

    values = [str(value) for value in i18n.MESSAGES[language].values()]
    stale = {text: old_names(text) for text in values if old_names(text)}
    assert not stale, f"{language} messages still name the UI the old way: {stale}"
    assert any(name in text.lower() for text in values), (
        f"No {language} message calls the terminal UI {name!r}")


@pytest.mark.parametrize("language,name", [("en", "dashboard"), ("ko", "대시보드")])
def test_open_without_a_terminal_tells_the_person_how_to_open_the_dashboard(
    git_history, tmp_path, monkeypatch, capsys, language, name,
):
    from foragent import cli

    case, main, commands = terminal_case(git_history, tmp_path, monkeypatch)
    config = replace(case.ctx.config, language=language)
    monkeypatch.setattr(cli, "load_config", lambda root: config)
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    capsys.readouterr()
    assert main(["gate", "watch", "--open", "--repo-root", str(case.root)]) == 0
    captured = capsys.readouterr()
    lines = (captured.out + captured.err).strip().splitlines()
    assert len(lines) == 1, f"One helpful line, not {lines}"
    assert name in lines[0].lower(), f"The line must name the {name}: {lines[0]!r}"
    assert not old_names(lines[0]), lines[0]
    assert "gate watch" in lines[0], "The runnable command stays `fa gate watch` for scripts"
    assert commands.calls() == []


def people_docs():
    pages = [ROOT / "README.md"]
    for page in sorted((ROOT / "docs").rglob("*.md")):
        if page.relative_to(ROOT / "docs").parts[0] not in ARCHIVES:
            pages.append(page)
    return pages


def test_readme_and_docs_pages_name_the_dashboard():
    pages = people_docs()
    assert len(pages) > 5, f"The docs scan found too few pages: {pages}"
    stale = {str(page.relative_to(ROOT)): old_names(page.read_text(encoding="utf-8"))
             for page in pages if old_names(page.read_text(encoding="utf-8"))}
    assert not stale, f"README/docs still name the UI the old way: {stale}"
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "dashboard" in readme.lower() and "대시보드" in readme, (
        "The README describes the UI in English and Korean, so both parts name it")
    for page in pages:
        text = page.read_text(encoding="utf-8")
        if "fa gate watch" in text or "fa dashboard" in text:
            assert NEW_NAME.search(text), f"{page.relative_to(ROOT)} describes the UI without naming it"


def test_skills_that_speak_to_the_person_name_the_dashboard():
    # "TUI" stays allowed here: skills use it for the kind of screen the
    # person's own project has, not for Foragent's UI.
    skills = sorted((ROOT / "skills").rglob("*.md"))
    assert skills, "No skills found"
    stale = {str(page.relative_to(ROOT)): old_names(page.read_text(encoding="utf-8"), OLD_UI_NAMES)
             for page in skills if old_names(page.read_text(encoding="utf-8"), OLD_UI_NAMES)}
    assert not stale, f"Skills still name the UI the old way: {stale}"
    for page in skills:
        text = page.read_text(encoding="utf-8")
        if "fa gate watch --open" in text:
            assert NEW_NAME.search(text), f"{page.relative_to(ROOT)} opens the UI without naming it"


def test_help_offers_fa_dashboard_as_a_person_command_with_the_same_flags(capsys):
    """Jin 2026-11-04 (q7, A): `fa dashboard` names the screen; `fa gate watch` stays for scripts."""
    top = fa_help()
    person = top[top.lower().index("person commands:"):top.lower().index("internal commands:")]
    assert re.search(r"^  dashboard\s", person, re.M), f"fa dashboard is listed under person commands: {person}"
    text = help_text(capsys, "dashboard")
    assert "--open" in text and "--once" in text and "--repo-root" in text, text
    assert "watch" in help_text(capsys, "gate"), "fa gate watch stays available for scripts"


def test_help_leads_with_slash_fa_as_the_only_thing_to_type():
    """Jin 2026-11-04: the one command a person learns is /fa; the help's first lines say so."""
    top = fa_help()
    head = top[:top.lower().index("person commands:")]
    assert "/fa" in head, f"The top of fa --help names /fa: {head!r}"
